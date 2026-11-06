# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class ProductController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/pricelists', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_pricelists(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            if sales_rep.available_pricelist_ids:
                pricelists = sales_rep.available_pricelist_ids.read(['id', 'name', 'currency_id'])
            else:
                pricelists = request.env['product.pricelist'].with_user(user).search_read([('company_id', 'in', [request.env.company.id, False])], ['id', 'name', 'currency_id'])
            return request.make_response(json.dumps({'success': True, 'pricelists': pricelists}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_pricelists: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/payment_terms', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_payment_terms(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            if sales_rep.available_payment_term_ids:
                payment_terms = sales_rep.available_payment_term_ids.read(['id', 'name'])
            else:
                payment_terms = request.env['account.payment.term'].with_user(user).search_read([('company_id', 'in', [request.env.company.id, False])], ['id', 'name'])
            return request.make_response(json.dumps({'success': True, 'payment_terms': payment_terms}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_payment_terms: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/products', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_products(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            page = int(request.params.get('page', 1))
            limit = int(request.params.get('limit', 50))
            query = request.params.get('query', '')
            location_id = request.params.get('location_id', '')
            
            # Determine location context and pricelist
            location_ctx = {}
            pricelist = None
            
            if sales_rep: 
                if sales_rep.default_location_id:
                     location_ctx = {'location': sales_rep.default_location_id.id}
                     _logger.info(f"Using location context: {location_ctx}")
                else:
                    _logger.warning("Sales Rep has no default_location_id")

                # Determine Partner/Pricelist Logic
                partner = None
                partner_id_param = request.params.get('partner_id')
                debug_log = []
                if partner_id_param:
                    try:
                        partner = request.env['res.partner'].with_user(user).browse(int(partner_id_param))
                        debug_log.append(f"Resolved partner from param: {partner.name} (ID: {partner.id})")
                    except ValueError:
                        debug_log.append(f"Invalid partner_id param: {partner_id_param}")
                        _logger.warning(f"Invalid partner_id param: {partner_id_param}")
                else:
                    # Look for active visit
                    active_visit = request.env['sales.rep.visit'].sudo().search([
                        ('sales_rep_id', '=', sales_rep.id),
                        ('state', '=', 'in_progress')
                    ], limit=1)
                    if active_visit:
                        partner = active_visit.partner_id
                        debug_log.append(f"Resolved partner from Active Visit: {partner.name} (ID: {partner.id})")
                        _logger.info(f"Using partner from active visit: {partner.name}")
                    else:
                        debug_log.append("No active visit found for sales rep.")

                # 1. Try explicit pricelist_id from params
                pricelist_id_param = request.params.get('pricelist_id')
                if pricelist_id_param:
                    try:
                        pricelist = request.env['product.pricelist'].sudo().browse(int(pricelist_id_param))
                        if pricelist.exists():
                            debug_log.append(f"Using explicit pricelist from param: {pricelist.name} (ID: {pricelist.id})")
                            _logger.info(f"Using explicit pricelist: {pricelist.name}")
                    except ValueError:
                         debug_log.append(f"Invalid pricelist_id param: {pricelist_id_param}")

                # 2. Fallback to Partner/Visit logic if no explicit pricelist
                if not pricelist:
                    if partner:
                         pricelist = partner.property_product_pricelist
                         debug_log.append(f"Using pricelist '{pricelist.name}' from partner property.")
                         _logger.info(f"Using pricelist: {pricelist.name} for partner {partner.name}")
                    else:
                         debug_log.append("No partner resolved, no pricelist selected.")

                # --- NEW: Promotions and Credit Points Logic ---
                credit_points = 0
                promotions = []
                if partner:
                    try:
                        # 1. Calculate Credit Points (Sum of points on all loyalty cards)
                        # Check if loyalty module is available
                        if 'loyalty.card' in request.env:
                            loyalty_cards = request.env['loyalty.card'].sudo().search([
                                ('partner_id', '=', partner.id)
                            ])
                            credit_points = sum(card.points for card in loyalty_cards)
                            debug_log.append(f"Calculated {credit_points} points from {len(loyalty_cards)} loyalty cards.")
                        else:
                            debug_log.append("Loyalty module not installed — skipping points.")

                        # 2. Fetch Active Promotions (Loyalty Programs)
                        # Filter by programs available for this partner/pricelist/company
                        programs_domain = [
                            ('active', '=', True),
                            ('company_id', 'in', [False, request.env.company.id]),
                            ('program_type', 'in', ['buy_x_get_y', 'promotion']),
                        ]
                        
                        # Filter by pricelist if available
                        if pricelist:
                            programs_domain.append('|')
                            programs_domain.append(('pricelist_ids', '=', False))
                            programs_domain.append(('pricelist_ids', 'in', [pricelist.id]))
                            debug_log.append(f"Filtering promotions for pricelist: {pricelist.name} (ID: {pricelist.id})")
                        else:
                            debug_log.append("No pricelist found for promotion filtering.")

                        debug_log.append(f"Promotion Search Domain: {str(programs_domain)}")
                        if 'loyalty.program' in request.env:
                            programs = request.env['loyalty.program'].sudo().search(programs_domain)
                        else:
                            programs = []
                            debug_log.append("Loyalty module not installed — skipping programs.")
                        
                        has_custom_promotion = 'priority' in request.env['loyalty.program']._fields
                        for program in programs:
                            p_lists = program.pricelist_ids.mapped('name')
                            
                            # Fetch Rules
                            rules = []
                            for rule in program.rule_ids:
                                rules.append({
                                    'id': rule.id,
                                    'code': rule.code,
                                    'minimum_amount': rule.minimum_amount,
                                    'minimum_qty': rule.minimum_qty,
                                    'reward_point_amount': rule.reward_point_amount,
                                    'reward_point_mode': rule.reward_point_mode,
                                    'product_ids': rule.product_ids.ids if rule.product_ids else [],
                                    'products': rule.product_ids.mapped('name') if rule.product_ids else [],
                                    'categories': [rule.product_category_id.name] if rule.product_category_id else [],
                                })

                            # Fetch Rewards
                            rewards = []
                            for reward in program.reward_ids:
                                rewards.append({
                                    'id': reward.id,
                                    'description': reward.description,
                                    'reward_type': reward.reward_type, # 'discount', 'product', 'shipping'
                                    'discount': reward.discount,
                                    'discount_mode': reward.discount_mode,
                                    'required_points': reward.required_points,
                                    'reward_product_id': reward.reward_product_id.id if reward.reward_product_id else False,
                                    'reward_product_name': reward.reward_product_id.name if reward.reward_product_id else False,
                                    'reward_product_qty': reward.reward_product_qty,
                                    'discount_line_product_id': reward.discount_line_product_id.id if reward.discount_line_product_id else False,
                                    'discount_line_product_name': reward.discount_line_product_id.display_name if reward.discount_line_product_id else False,
                                })

                            promotions.append({
                                'id': program.id,
                                'name': program.name,
                                'program_type': program.program_type,
                                'trigger': program.trigger,
                                'priority': getattr(program, 'priority', 999),
                                'can_be_shared': getattr(program, 'can_be_shared', True),
                                'is_cash': getattr(program, 'is_cash', False),
                                'is_auto': getattr(program, 'is_auto', True) if has_custom_promotion else False,
                                'pricelist_ids': program.pricelist_ids.ids,
                                'rules': rules,
                                'rewards': rewards,
                            })
                        debug_log.append(f"Found {len(promotions)} active promotions.")

                    except Exception as e:
                        _logger.error(f"Error fetching loyalty info: {e}")
                        debug_log.append(f"Error fetching loyalty info: {str(e)}")
                # -----------------------------------------------

                # available_pricelists for the sales rep
                available_pricelists = sales_rep.available_pricelist_ids.read(['id', 'name'])

                # Optimization: If loyalty_only is requested, return early
                if request.params.get('loyalty_only') == 'true':
                    response_data = {
                        'success': True, 
                        'products': [],
                        'applied_pricelist': pricelist.name if pricelist else "Default/List Price",
                        'available_pricelists': available_pricelists,
                        'debug_info': debug_log,
                        'credit_points': credit_points,
                        'promotions': promotions
                    }
                    return request.make_response(json.dumps(response_data, default=str), headers={'Content-Type': 'application/json'})


            # 1. Fetch Templates
            domain = [('sale_ok', '=', True), ('purchase_ok', '=', True), ('active', '=', True), ('is_storable', '=', True)]
            if query:
                domain.append(('name', 'ilike', query))
            
            offset = (page - 1) * limit
            
            # Use context for free_qty calculation on variants/templates if strictly needed (less specific on template)
            tmpl_model = request.env['product.template'].with_context(**location_ctx)
            
            templates = tmpl_model.sudo().search_read(domain, 
                ['id', 'name', 'list_price', 'image_512', 'image_1920', 'uom_id', 'taxes_id', 'attribute_line_ids', 'product_variant_ids', 'virtual_available'],
                limit=limit, offset=offset)

            # Collect UoM IDs to fetch categories
            uom_ids = set()
            for t in templates:
                if t.get('uom_id'):
                    uom_ids.add(t['uom_id'][0])

            template_ids = [t['id'] for t in templates]
            
            if not template_ids:
                 return request.make_response(json.dumps({'success': True, 'products': []}, default=str), headers={'Content-Type': 'application/json'})

            # 2. Fetch Variants for these templates
            product_model = request.env['product.product'].with_context(**location_ctx)
            variants = product_model.sudo().search_read(
                [('product_tmpl_id', 'in', template_ids), ('active', '=', True)],
                ['id', 'name', 'display_name', 'lst_price', 'image_512', 'image_1920', 'free_qty', 'virtual_available', 'uom_id', 'product_tmpl_id']
            )

            for v in variants:
                if v.get('uom_id'):
                    uom_ids.add(v['uom_id'][0])

            # Fetch UoM Categories
            uom_category_map = {}
            if uom_ids:
                uoms = request.env['uom.uom'].sudo().search_read([('id', 'in', list(uom_ids))], ['id', 'category_id'])
                uom_category_map = {u['id']: u['category_id'][0] for u in uoms if u['category_id']}
            
            # Fix variant images
            for variant in variants:
                if variant.get('image_1920'):
                    variant['image_1920'] = variant['image_1920'].decode('utf-8')
                if variant.get('image_512'):
                    variant['image_512'] = variant['image_512'].decode('utf-8')
                
                # Add uom_category_id
                if variant.get('uom_id'):
                    variant['uom_category_id'] = uom_category_map.get(variant['uom_id'][0])
                else:
                    variant['uom_category_id'] = False

            # 3. Fetch Attribute Details
            line_ids = [l_id for t in templates for l_id in t.get('attribute_line_ids', [])]
            lines_by_template = {}
            if line_ids:
                lines = request.env['product.template.attribute.line'].sudo().search_read(
                    [('id', 'in', line_ids)],
                    ['product_tmpl_id', 'attribute_id', 'value_ids']
                )
                
                all_value_ids = list(set([v_id for l in lines for v_id in l['value_ids']]))
                values_map = {}
                if all_value_ids:
                    values_data = request.env['product.attribute.value'].sudo().search_read(
                        [('id', 'in', all_value_ids)],
                        ['id', 'name', 'attribute_id']
                    )
                    values_map = {v['id']: v for v in values_data}

                for l in lines:
                    tmpl_id = l['product_tmpl_id'][0]
                    if tmpl_id not in lines_by_template:
                        lines_by_template[tmpl_id] = []
                    
                    resolved_values = []
                    for v_id in l['value_ids']:
                        if v_id in values_map:
                            resolved_values.append({
                                'id': values_map[v_id]['id'],
                                'name': values_map[v_id]['name']
                            })
                    
                    lines_by_template[tmpl_id].append({
                        'attribute_id': l['attribute_id'][0],
                        'attribute_name': l['attribute_id'][1],
                        'values': resolved_values
                    })

            # 4. Consolidate Data and Apply Pricing
            
            # Create maps for record browsing to use pricelist methods
            if pricelist:
                templates_map = {t.id: t for t in tmpl_model.browse(template_ids)}
                variant_ids = [v['id'] for v in variants]
                variants_map = {v.id: v for v in product_model.browse(variant_ids)}

            for template in templates:
                # Attach Attributes
                template['attribute_details'] = lines_by_template.get(template['id'], [])

                # Add uom_category_id
                if template.get('uom_id'):
                    template['uom_category_id'] = uom_category_map.get(template['uom_id'][0])
                else:
                    template['uom_category_id'] = False
                
                # Attach Variants
                # Filter variants for this template
                # product_tmpl_id is (id, name)
                template_variants = [v for v in variants if v['product_tmpl_id'][0] == template['id']]
                template['variants'] = template_variants
                
                # Sum free_qty
                template['free_qty'] = sum(v.get('free_qty', 0.0) for v in template_variants)

                # Update Prices with Pricelist
                if pricelist:
                    tmpl_rec = templates_map.get(template['id'])
                    if tmpl_rec:
                        # For templates, we use the first variant or the template itself if single variant
                        # _get_product_price works on product.product (variant) mostly for accurate price
                        # But can try on template. Odoo standards usually price per product.
                        # Let's use the first available variant to get a representative price for the template
                        # Or standard logic:
                        price = pricelist._get_product_price(tmpl_rec, 1.0)
                        template['list_price'] = price
                        template['lst_price'] = price
                else:
                     # Ensure price availability for frontend (it expects lst_price)
                    if 'list_price' in template and 'lst_price' not in template:
                        template['lst_price'] = template['list_price']

                # Update Variant Prices
                for v_dict in template['variants']:
                     if pricelist:
                         var_rec = variants_map.get(v_dict['id'])
                         if var_rec:
                             # Calculate price for 1 unit
                             price = pricelist._get_product_price(var_rec, 1.0)
                             v_dict['lst_price'] = price

                if template.get('image_1920'):
                    template['image_1920'] = template['image_1920'].decode('utf-8')
                if template.get('image_512'):
                    template['image_512'] = template['image_512'].decode('utf-8')
            
            # Get available pricelists for the sales rep again for the full response
            available_pricelists = []
            if sales_rep:
                available_pricelists = sales_rep.available_pricelist_ids.read(['id', 'name'])

            response_data = {
                'success': True, 
                'products': templates,
                'applied_pricelist': pricelist.name if pricelist else "Default/List Price",
                'available_pricelists': available_pricelists,
                'debug_info': debug_log,
                'credit_points': credit_points,
                'promotions': promotions
            }
            return request.make_response(json.dumps(response_data, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_products: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
