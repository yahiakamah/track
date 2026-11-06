# -*- coding: utf-8 -*-
"""Download (server -> device) half of the mobile sync."""
from odoo import http, fields, _
from odoo.exceptions import UserError
from odoo.http import request
import json
import logging
import base64
import traceback
from datetime import datetime, timedelta
from psycopg2 import IntegrityError

_logger = logging.getLogger(__name__)


class SyncDownloadMixin:
    """Mixed into SyncController (controllers/sync.py); no routes here."""

    def _rep_partner_scope(self, sales_rep, route_partner_ids):
        """Customers the rep works with: route customers + anyone the rep sold to."""
        ids = set(route_partner_ids or [])
        if sales_rep.user_id:
            for partner, in request.env['sale.order'].sudo()._read_group(
                    [('user_id', '=', sales_rep.user_id.id), ('state', '=', 'sale'),
                     ('company_id', '=', sales_rep.company_id.id)], ['partner_id']):
                ids.add(partner.id)
        return list(ids)

    def _rep_partner_debts(self, sales_rep, route_partner_ids):
        """[{id, total_due}] for route customers + anyone the rep sold to."""
        ids = self._rep_partner_scope(sales_rep, route_partner_ids)
        due = request.env['res.partner']._sales_rep_due_map(ids, sales_rep.company_id.id)
        return [{'id': pid, 'total_due': amount} for pid, amount in due.items()]

    def _rep_open_invoices(self, sales_rep, partner_ids):
        """Unpaid customer invoices of those customers (whoever issued them), so the
        collection screen can list them and the rep picks the one being paid."""
        if not partner_ids:
            return []
        moves = request.env['account.move'].sudo().search([
            ('partner_id', 'in', partner_ids), ('company_id', '=', sales_rep.company_id.id),
            ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')), ('amount_residual', '>', 0),
        ], order='invoice_date_due, invoice_date, id', limit=2000)
        return [{
            'id': m.id, 'name': m.name, 'partner_id': m.partner_id.id,
            'invoice_date': m.invoice_date, 'invoice_date_due': m.invoice_date_due,
            'amount_total': m.amount_total, 'amount_residual': m.amount_residual,
            'payment_state': m.payment_state,
            'origin': m.invoice_origin or '',
        } for m in moves]

    def _process_downloads(self, sales_rep, last_sync_date, user, capabilities=()):
        # Format last_sync_date for Odoo domain [YYYY-MM-DD HH:MM:SS]
        # mobile app might send it in various ISO formats
        since = False
        if last_sync_date:
            try:
                # Odoo expects UTC strings. If the app sends ISO, we might need to truncate/parse.
                # Common format from JS: 2026-02-19T04:06:56.000Z -> 2026-02-19 04:06:56
                since = last_sync_date.replace('T', ' ').split('.')[0].replace('Z', '')
                _logger.info(f"Sync: Fetching updates since {since}")
            except Exception as e:
                _logger.warning(f"Failed to parse last_sync_date {last_sync_date}: {e}")

        def get_delta_domain(base_domain):
            if since:
                return base_domain + [('write_date', '>', since)]
            return base_domain
        
        downloads = {
            'sales_rep_profile': {
                'id': sales_rep.id,
                'name': sales_rep.name,
                'email': sales_rep.email,
                'user_id': sales_rep.user_id.id,
                'company_id': sales_rep.company_id.id,
                'company_name': sales_rep.company_id.name,
                'sale_team_id': sales_rep.user_id.sale_team_id.id if sales_rep.user_id.sale_team_id else False,
                'image_url': f"data:image/png;base64,{sales_rep.user_id.image_128.decode('utf-8') if isinstance(sales_rep.user_id.image_128, bytes) else sales_rep.user_id.image_128}" if (sales_rep.user_id and sales_rep.user_id.image_128) else None,
                'is_supervisor': sales_rep.is_supervisor,
                'is_manager': sales_rep.is_manager,
                'supervisor_id': sales_rep.supervisor_id.id if sales_rep.supervisor_id else False,
                'auto_delivery': sales_rep.auto_delivery,
                'auto_receive': sales_rep.auto_receive,
                'invoice_journal_id': sales_rep.invoice_journal_id.id if sales_rep.invoice_journal_id else False,
                'default_location_id': sales_rep.default_location_id.id if sales_rep.default_location_id else False,
                'default_location_name': sales_rep.default_location_id.display_name if sales_rep.default_location_id else False,
                'default_location_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.default_location_ids],
                'return_location_id': sales_rep.return_location_id.id if sales_rep.return_location_id else False,
                'return_location_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.return_location_ids],
                'product_category_ids': sales_rep.product_category_ids.ids,
                'product_categories': [{'id': c.id, 'name': c.display_name} for c in sales_rep.product_category_ids],
                'payment_method_ids': sales_rep.payment_method_ids.ids,
                'default_pricelist_id': sales_rep.available_pricelist_ids[0].id if sales_rep.available_pricelist_ids else False,
                'payment_term_id': sales_rep.available_payment_term_ids[0].id if sales_rep.available_payment_term_ids else False,
                'discount_product_id': sales_rep.discount_product_id.id if sales_rep.discount_product_id else False,
                'location_request_id': sales_rep.location_request_id.id if sales_rep.location_request_id else False,
                'location_request_name': sales_rep.location_request_id.display_name if sales_rep.location_request_id else False,
                'location_request_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.location_request_ids],
                'location_general_return_id': sales_rep.location_general_return.id if hasattr(sales_rep, 'location_general_return') and sales_rep.location_general_return else False,
                'location_general_return_name': sales_rep.location_general_return.display_name if hasattr(sales_rep, 'location_general_return') and sales_rep.location_general_return else False,
                'location_general_return_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.location_general_return_ids] if hasattr(sales_rep, 'location_general_return_ids') else [],
                # Access Rights
                'access_cash_balance': sales_rep.access_cash_balance,
                'access_storage': sales_rep.access_storage,
                'access_returns': sales_rep.access_returns,
                'access_general_return': sales_rep.access_general_return if hasattr(sales_rep, 'access_general_return') else False,
                'access_confirm_quotation': sales_rep.access_confirm_quotation,
                'access_delivery': sales_rep.access_delivery,
                'access_payment': sales_rep.access_payment,
                'access_discount': sales_rep.access_discount,
                'access_inventory_adjustment': sales_rep.access_inventory_adjustment,
                'access_sales_report': sales_rep.access_sales_report,
                'access_customer_debt_report': sales_rep.access_customer_debt_report,
                'access_collection_report': sales_rep.access_collection_report,
                'access_journal_report': sales_rep.access_journal_report,
                'access_requests': sales_rep.access_requests,
                'access_cancel_quotation': sales_rep.access_cancel_quotation,
                'access_visit_order': sales_rep.access_visit_order,
                'access_visit_payment': sales_rep.access_visit_payment,
                'access_create_customer': sales_rep.access_create_customer,
                'access_mock_location': sales_rep.access_mock_location,
                'access_force_logout_on_mock_location': sales_rep.access_force_logout_on_mock_location,
                'access_developer_mode': sales_rep.access_developer_mode,
                'access_force_logout_on_developer_mode': sales_rep.access_force_logout_on_developer_mode,
            },
            'routes': [],
            'route_customers': [],
            'products': [],
            'visits': [],
            'collections': [],
            'invoices': [],
            'invoice_lines': [],
            'payments': [],
            'payment_methods': [],
            'sales_orders': [],
            'sales_order_lines': [],
            'payment_journals': [],
            'pricelists': [],
            'pricelist_items': [],
            'payment_terms': [],
            'return_reasons': [],
            'uom_categories': [],
            'uoms': [],
            'stock_requests': [],
            'stock_pickings': [],
            'stock_moves': [],
        }
        
        # 1. Routes (Active and today/future)
        domain = [
            ('sales_rep_id', '=', sales_rep.id),
            ('state', 'in', ['in_progress']),
            ('company_id', '=', sales_rep.company_id.id),
        ]
        
        # We always fetch ALL active routes regardless of last_sync_date to ensure 
        # reliability for current operations. Delta sync is applied to other models.
        routes = request.env['sales.rep.route'].with_user(user).search_read(
            domain, 
            ['id', 'name', 'date', 'state', 'start_time', 'end_time', 'sales_rep_id']
        )
        downloads['routes'] = routes
        
        # 2. Route Customers
        # Fetch all customers for the routes we just found
        active_route_ids = [r['id'] for r in routes]
        if active_route_ids:
            rc_domain = [('route_id', 'in', active_route_ids)]
            customers = request.env['sales.route.customer'].with_user(user).search_read(
                rc_domain,
                ['id', 'route_id', 'partner_id', 'sequence', 'state', 'visit_start_time', 'visit_end_time', 'visit_notes', 'visit_type_id']
            )
        else:
            customers = []
            
        # Enrich with partner info (address, coords, phone)
        partner_ids = [c['partner_id'][0] for c in customers if c['partner_id']]
        # Build field list — include financial limit fields if the module is installed
        partner_fields = ['id', 'name', 'display_name', 'street', 'city', 'state_id', 'country_id', 'area', 'phone', 'email', 'is_cash', 'visit_latitude', 'visit_longitude', 'property_product_pricelist', 'property_payment_term_id', 'credit_limit', 'enable_location', 'location_radius', 'mobile_local_id', 'category_id']
        has_total_due_field = 'total_due' in request.env['res.partner']._fields
        if has_total_due_field:
            partner_fields.append('total_due')
        if 'sale_credit_limit' in request.env['res.partner']._fields:
            partner_fields += ['sale_credit_limit', 'allow_over_sale_credit', 'sale_credit_used']
        partners = request.env['res.partner'].with_user(user).search_read(
            [('id', 'in', partner_ids), ('company_id', 'in', [sales_rep.company_id.id, False])],
            partner_fields
        )
        due_map = request.env['res.partner']._sales_rep_due_map([p['id'] for p in partners], sales_rep.company_id.id)
        for p in partners:
            if p.get('country_id'): p['country_id'] = p['country_id'][0]
            if p.get('state_id'): p['state_id'] = p['state_id'][0]
            if p.get('property_payment_term_id'): p['property_payment_term_id'] = p['property_payment_term_id'][0]
            if p.get('property_product_pricelist'): p['property_product_pricelist'] = p['property_product_pricelist'][0]
            p['total_due'] = due_map.get(p['id'], 0.0)
        partner_map = {p['id']: p for p in partners}
            
        loyalty_map = {}
        try:
            loyalty_cards = request.env['loyalty.card'].with_user(user).search_read(
                [('partner_id', 'in', partner_ids)],
                ['partner_id', 'points']
            )
            for card in loyalty_cards:
                    pid = card['partner_id'][0]
                    if pid not in loyalty_map:
                        loyalty_map[pid] = 0
                    loyalty_map[pid] += card['points']
        except Exception as e:
            _logger.warning(f"Sync: Loyalty module not found or accessible: {e}")

        for c in customers:
            pid = c['partner_id'][0] if c['partner_id'] else None
            if pid and pid in partner_map:
                p = partner_map[pid]
                c['name'] = p['name'] # Manually add name from partner
                c['address'] = f"{p.get('street') or ''}, {p.get('city') or ''}".strip(', ')
                c['phone'] = p.get('phone') or p.get('mobile')
                c['email'] = p.get('email')
                # Prioritize custom fields (visit_*) over standard (partner_*)
                c['latitude'] = p.get('visit_latitude') or p.get('partner_latitude')
                c['longitude'] = p.get('visit_longitude') or p.get('partner_longitude')
                c['radius'] = p.get('location_radius', 0.0)
                c['enable_location'] = p.get('enable_location', True)
                c['location_radius'] = p.get('location_radius', 0.0)
                c['is_cash'] = p.get('is_cash', True)
                c['state_id'] = p.get('state_id')
                c['country_id'] = p.get('country_id')
                c['area'] = p.get('area')
                c['category_id_json'] = json.dumps(p.get('category_id') or [])

                # Handle visit_type_id (Tuple -> Name)
                if c.get('visit_type_id') and isinstance(c['visit_type_id'], (list, tuple)):
                    c['visit_type_name'] = c['visit_type_id'][1]
                    c['visit_type_id'] = c['visit_type_id'][0]
                else:
                    c['visit_type_name'] = None
                    c['visit_type_id'] = c.get('visit_type_id') or False
                
                # Add full partner details for local record creation
                c['partner_data'] = p
                c['partner_data']['loyalty_points'] = loyalty_map.get(pid, 0)
            else:
                c['name'] = 'Unknown Customer'
        
        downloads['route_customers'] = customers

        # 2b. Promotions (Loyalty Programs)
        try:
             # Check if loyalty module is available
             if 'loyalty.program' not in request.env:
                 _logger.info("Sync: Loyalty module not installed, skipping promotions.")
                 downloads['promotions'] = []
             else:
                  
                 # Fetch active programs with rules and rewards. Also fetch inactive ones if they changed recently so app can archive them.
                 programs_domain = [
                     ('program_type', 'in', ['buy_x_get_y', 'promotion', 'tier']),
                     '|', ('active', '=', True), ('active', '=', False), 
                     ('company_id', 'in', [False, sales_rep.company_id.id])
                 ]
                 programs = request.env['loyalty.program'].with_user(user).search(get_delta_domain(programs_domain))
                 
                 promotions_list = []
                 has_custom_promotion = 'priority' in request.env['loyalty.program']._fields
                 for program in programs:
                     if not program.active:
                         promotions_list.append({
                             'id': program.id,
                             'is_archived': True,
                         })
                         continue
                     
                     # Serialize Rules
                     rules = []
                     for rule in program.rule_ids:
                         rules.append({
                             'id': rule.id,
                             'code': rule.code,
                             'minimum_amount': rule.minimum_amount,
                             'minimum_qty': rule.minimum_qty,
                             'reward_point_amount': rule.reward_point_amount,
                             'reward_point_mode': rule.reward_point_mode,
                             'product_ids': rule.product_ids.ids,
                             'products': rule.product_ids.mapped('name'),
                             'categories': rule.product_category_id.mapped('name'),
                         })
    
                     # Serialize Rewards
                     rewards = []
                     for reward in program.reward_ids:
                         rewards.append({
                             'id': reward.id,
                             'description': reward.description,
                             'reward_type': reward.reward_type,
                             'discount': reward.discount,
                             'discount_mode': reward.discount_mode,
                             'required_points': reward.required_points,
                             'reward_product_id': reward.reward_product_id.id if reward.reward_product_id else False,
                             'reward_product_name': reward.reward_product_id.name if reward.reward_product_id else False,
                             'reward_product_qty': reward.reward_product_qty if hasattr(reward, 'reward_product_qty') else 1,
                         })

                     # Serialize Tiers
                     tiers = []
                     if hasattr(program, 'program_tier_ids'):
                         for tier in program.program_tier_ids:
                             tiers.append({
                                 'id': tier.id,
                                 'name': tier.name,
                                 'minimum_amount': tier.minimum_amount,
                                 'maximum_amount': tier.maximum_amount,
                                 'trigger_type': tier.trigger_type,
                                 'reward_type': tier.reward_type,
                                 'discount': tier.reward_amount,
                                 'bonus_product_id': tier.reward_product_id.id if tier.reward_product_id else False,
                                 'bonus_product_name': tier.reward_product_id.name if tier.reward_product_id else False,
                                 'bonus_product_qty': tier.qty,
                                 'rule_product_id': tier.rule_product_id.id if tier.rule_product_id else False,
                                 'rule_uom_id': tier.rule_uom_id.id if tier.rule_uom_id else False,
                             })
    
                     promotions_list.append({
                         'id': program.id,
                         'name': program.name,
                         'program_type': program.program_type,
                         'trigger': program.trigger,
                         'priority': getattr(program, 'priority', 999),
                         'can_be_shared': getattr(program, 'can_be_shared', True),
                         'is_cash': getattr(program, 'is_cash', False),
                         'is_auto': getattr(program, 'is_auto', True) if has_custom_promotion else False,
                         'tiers_type': getattr(program, 'tiers_type', False),
                         'discount_product_id': program.discount_product_id.id if hasattr(program, 'discount_product_id') and program.discount_product_id else False,
                         'limit_partner_ids': program.limit_partner_ids.ids if hasattr(program, 'limit_partner_ids') else [],
                         'limit_partner_category_ids': program.limit_partner_category_ids.ids if hasattr(program, 'limit_partner_category_ids') else [],
                         'limit_country_ids': program.limit_country_ids.ids if hasattr(program, 'limit_country_ids') else [],
                         'limit_state_ids': program.limit_state_ids.ids if hasattr(program, 'limit_state_ids') else [],
                         'limit_area_names': program.limit_area_ids.mapped('name') if hasattr(program, 'limit_area_ids') else [],
                         'pricelist_ids': program.pricelist_ids.ids,
                         'date_from': program.date_from or False,
                         'date_to': program.date_to or False,
                         'json_data': json.dumps({'rules': rules, 'rewards': rewards, 'tiers': tiers})
                     })
                 
                 downloads['promotions'] = promotions_list
                 _logger.info(f"Sync: Downloaded {len(promotions_list)} promotions")
                 if promotions_list:
                     _logger.info(f"Sync: Synced Promotion Names: {[p.get('name') for p in promotions_list]}")
        except Exception as e:
             _logger.warning(f"Sync: Failed to fetch promotions: {e}")
             downloads['promotions'] = []


        # 3. Products
        # Stock levels change without touching the product's write_date, so
        # quantities are always sent for every product. What changes is the
        # heavy part (catalog fields + image):
        #   - app without 'product_delta' capability / first sync: full list
        #     with images (legacy behaviour);
        #   - app with 'product_delta' on a delta sync: full records only for
        #     products changed since the last sync, quantities for the rest in
        #     'product_qtys', and the ids still valid in 'product_ids' so the
        #     app can retire products that were archived or left the rep's
        #     categories.
        product_delta = bool(since) and 'product_delta' in (capabilities or ())
        changed_at = sales_rep.catalog_changed_at
        if product_delta and changed_at and fields.Datetime.to_string(changed_at) > since:
            # rep settings changed (categories, locations...): the visible
            # catalog may have changed without any product being written
            product_delta = False
        
        p_domain = [
            ('active', '=', True),
            ('company_id', 'in', [sales_rep.company_id.id, False])
        ]
        
        # Include service products if it's the configured discount product
        if sales_rep.discount_product_id:
            p_domain += ['|', ('is_storable', '=', True), ('id', '=', sales_rep.discount_product_id.id)]
        else:
            p_domain += [('is_storable', '=', True)]
        
        # Filter products by representative's allowed categories if any
        if sales_rep.product_category_ids:
            p_domain.append(('categ_id', 'child_of', sales_rep.product_category_ids.ids))
        
        # Add location context so free_qty is calculated for the rep's default location
        product_context = {}
        if sales_rep.default_location_id:
            product_context['location'] = sales_rep.default_location_id.id
            _logger.info(f"Sync: Fetching products for location: {sales_rep.default_location_id.name}")
            # We no longer strictly filter products by stock > 0 so that 0-stock items
            # are also visible in the mobile app's storage and catalog.
            stock_field = 'free_qty'

        # We use p_domain directly (skipping get_delta_domain) for products
        products = request.env['product.product'].with_user(user).with_context(**product_context).search_read(
            p_domain, 
            ['id', 'name', 'display_name', 'default_code', 'barcode', 'list_price', 'uom_id', 'categ_id',
             'product_tmpl_id', 'type', 'is_storable', 'invoice_policy', 'taxes_id', 'free_qty', 'active'])
        if product_delta:
            changed_ids = set(request.env['product.product'].with_user(user).search(
                p_domain + ['|', ('write_date', '>', since), ('product_tmpl_id.write_date', '>', since)]).ids)
        else:
            changed_ids = {p['id'] for p in products}
        image_map = {}
        if changed_ids:
            for rec in request.env['product.product'].with_user(user).browse(list(changed_ids)).read(['image_512']):
                image_map[rec['id']] = rec['image_512']
        for p in products:
            if p['id'] in image_map:
                p['image_512'] = image_map[p['id']]

        # Calculate stock levels (free quantity) for all storable products across all assigned default locations
        location_ids = sales_rep.default_location_ids.ids
        product_location_quants = {}
        if location_ids and products:
            locations = request.env['stock.location'].sudo().browse(location_ids)
            location_names = {loc.id: loc.display_name or loc.name for loc in locations}
            
            product_ids = [p['id'] for p in products]
            quants = request.env['stock.quant'].sudo().search_read(
                [('product_id', 'in', product_ids), ('location_id', 'in', location_ids)],
                ['product_id', 'location_id', 'quantity', 'reserved_quantity']
            )
            for q in quants:
                pid = q['product_id'][0]
                loc_id = q['location_id'][0]
                qty = q.get('quantity', 0.0) - q.get('reserved_quantity', 0.0)
                loc_name = location_names.get(loc_id, f"Location #{loc_id}")
                
                if pid not in product_location_quants:
                    product_location_quants[pid] = []
                product_location_quants[pid].append({
                    'location_id': loc_id,
                    'location_name': loc_name,
                    'quantity': qty
                })
        _logger.info("Sync: Calculated product_location_quants mapping")

        # Collect UoM IDs to fetch categories
        product_uom_ids = set()
        for p in products:
            if p.get('uom_id'):
                product_uom_ids.add(p['uom_id'][0])

        # Fetch UoM Categories for products
        product_uom_category_map = {}
        if product_uom_ids:
            uoms_for_cat = request.env['uom.uom'].sudo().search_read([('id', 'in', list(product_uom_ids))], ['id', 'category_id'])
            product_uom_category_map = {u['id']: u['category_id'][0] for u in uoms_for_cat if u['category_id']}
        
        
        # Pre-fetch taxes for enrichment
        all_tax_ids = set()
        for p in products:
            if p.get('taxes_id'):
                all_tax_ids.update(p['taxes_id'])
        
        tax_map = {}
        if all_tax_ids:
            taxes = request.env['account.tax'].with_user(user).search_read(
                [('id', 'in', list(all_tax_ids))],
                ['id', 'name', 'amount', 'amount_type']
            )
            for t in taxes:
                tax_map[t['id']] = t

        # Map fields for SQLite
        for p in products:
            if p.get('image_512'):
                img_val = p['image_512'].decode('utf-8') if isinstance(p['image_512'], bytes) else p['image_512']
                p['image_url'] = f"data:image/png;base64,{img_val}"
            else:
                p['image_url'] = None
            if 'image_512' in p:
                del p['image_512']
            
            # Handle product_tmpl_id (Tuple -> ID)
            if p.get('product_tmpl_id'):
                p['product_tmpl_id'] = p['product_tmpl_id'][0] if isinstance(p['product_tmpl_id'], (list, tuple)) else p['product_tmpl_id']
            else:
                p['product_tmpl_id'] = False

            # Handle uom_id and category_id
            if p.get('uom_id') and isinstance(p['uom_id'], (list, tuple)):
                uom_id_val = p['uom_id'][0]
                p['uom_name'] = p['uom_id'][1]
                p['uom_id'] = uom_id_val
                p['uom_category_id'] = product_uom_category_map.get(uom_id_val)
            elif p.get('uom_id'):
                # Already an ID (maybe from previous processing or different Odoo version behavior)
                uom_id_val = p['uom_id']
                p['uom_name'] = p.get('uom_name') or 'Unit'
                p['uom_category_id'] = product_uom_category_map.get(uom_id_val)
            else:
                p['uom_id'] = False
                p['uom_name'] = 'Unit'
                p['uom_category_id'] = False

            # Handle categ_id (Tuple -> ID)
            if p.get('categ_id') and isinstance(p['categ_id'], (list, tuple)):
                p['categ_name'] = p['categ_id'][1]
                p['categ_id'] = p['categ_id'][0]
            else:
                p['categ_name'] = None
                p['categ_id'] = p.get('categ_id') or False

            # Handle taxes_id: Enrich with actual tax amount/name
            if p.get('taxes_id'):
                p['taxes_id'] = [tax_map.get(tid) for tid in p['taxes_id'] if tid in tax_map]
            else:
                p['taxes_id'] = []
            
            # Handle type mapping for mobile app compatibility (detailed_type)
            if p.get('is_storable'):
                p['detailed_type'] = 'product'
            else:
                p['detailed_type'] = p.get('type')
            
            # Ensure text fields are None if False (Odoo behavior)
            if p.get('detailed_type') is False:
                p['detailed_type'] = None
            if p.get('invoice_policy') is False:
                p['invoice_policy'] = None

            # Add free_qtys list mapping
            p['free_qtys'] = product_location_quants.get(p['id'], [])
            
        if product_delta:
            downloads['product_ids'] = [p['id'] for p in products]
            downloads['product_qtys'] = [{'id': p['id'], 'free_qtys': p['free_qtys']}
                                         for p in products if p['id'] not in changed_ids]
            products = [p for p in products if p['id'] in changed_ids]
        downloads['products'] = products

        # 4. Visits (Completed visits from last sync date)
        visit_domain = [
            ('sales_rep_id', '=', sales_rep.id),
            ('state', '=', 'completed'),
            ('visit_time', '>=', last_sync_date),
            ('company_id', '=', sales_rep.company_id.id),
        ]
        visits = request.env['sales.rep.visit'].with_user(user).search_read(
            get_delta_domain(visit_domain),
            ['id', 'name', 'sales_rep_id', 'route_id', 'route_customer_id', 'partner_id', 'visit_type', 'state', 'visit_result', 'notes', 'visit_location_lat', 'visit_location_long', 'planned_time', 'visit_time']
        )
        downloads['visits'] = visits

        # 5. Collections (Fetch last 30 days of collections and payments)
        thirty_days_ago = fields.Datetime.now() - timedelta(days=30)
        
        # Get active routes for the rep in this period to broaden search
        active_routes = request.env['sales.rep.route'].sudo().search([
            ('sales_rep_id', '=', sales_rep.id),
            ('date', '>=', thirty_days_ago.date())
        ])
        route_ids = active_routes.ids
        
        collection_domain = [
            ('collection_date', '>=', thirty_days_ago),
            '|', ('sales_rep_id', '=', sales_rep.id), ('route_id', 'in', route_ids)
        ]
        
        _logger.info(f"Sync: Fetching collections/payments for sales rep {sales_rep.name} since {thirty_days_ago}")
        # collections_raw = request.env['sales.rep.collection'].with_user(user).search(collection_domain)
        collections_raw = []
        
        collections = []
        seen_payment_ids = set()
        
        for col in collections_raw:
            if col.payment_id:
                seen_payment_ids.add(col.payment_id.id)
                
            col_data = {
                'id': col.id,
                'name': col.name,
                'sales_rep_id': col.sales_rep_id.id or sales_rep.id,
                'route_id': col.route_id.id or (col.visit_id.route_id.id if col.visit_id else False),
                'route_customer_id': col.route_customer_id.id,
                'partner_id': col.partner_id.id,
                'visit_id': col.visit_id.id,
                'collection_date': col.collection_date,
                'amount': col.amount,
                'payment_method': col.payment_method,
                'payment_method_id': col.payment_method_id.id,
                'state': col.state,
                'mobile_local_id': col.mobile_local_id,
                'journal_id': col.payment_id.journal_id.id if col.payment_id else False
            }
            # Fallback for route_id if still missing but we can deduce it
            if not col_data['route_id'] and col_data['visit_id']:
                visit = request.env['sales.rep.visit'].sudo().browse(col_data['visit_id'])
                col_data['route_id'] = visit.route_id.id
                
            collections.append(col_data)
        
        # Also fetch direct account.payment records that might not have a sales.rep.collection
        payment_domain = [
            ('date', '>=', thirty_days_ago.date()),
            ('state', '!=', 'cancel'),
            '|', ('route_id', 'in', route_ids), ('partner_id.user_id', '=', user.id)
        ]
        orphan_payments = request.env['account.payment'].with_user(user).search([
            ('id', 'not in', list(seen_payment_ids))
        ] + payment_domain)
        
        for pay in orphan_payments:
            # Map Odoo payment journal type to mobile payment method selection
            pay_method = 'cash'
            if pay.journal_id.type == 'bank':
                pay_method = 'bank_transfer'
            elif 'card' in (pay.journal_id.name or '').lower():
                pay_method = 'card'
            elif 'check' in (pay.journal_id.name or '').lower():
                pay_method = 'check'
                
            collections.append({
                'id': 2000000 + pay.id, # Synthetic Integer ID to fit SQLite INT
                'name': pay.name or f"Payment {pay.id}",
                'sales_rep_id': sales_rep.id,
                'route_id': pay.route_id.id or False,
                'route_customer_id': pay.route_customer_id.id or False,
                'partner_id': pay.partner_id.id,
                'visit_id': pay.visit_id.id or False,
                'collection_date': pay.date,
                'amount': pay.amount,
                'payment_method': pay_method,
                'payment_method_id': False,
                'state': 'confirmed' if pay.state == 'posted' else 'draft',
                'mobile_local_id': pay.mobile_local_id or False,
                'journal_id': pay.journal_id.id
            })
            
        downloads['collections'] = collections

        # 6. Invoices (Created or modified from last sync date)
        invoice_domain = [
            ('state', 'in', ['posted', 'paid']),
            ('company_id', '=', sales_rep.company_id.id),
        ]
        invoices = request.env['account.move'].with_user(user).search_read(
            get_delta_domain(invoice_domain),
            ['id', 'name', 'move_type', 'partner_id', 'invoice_date', 'amount_total', 'amount_residual', 'state', 'invoice_line_ids', 'payment_state']
        )
        downloads['invoices'] = invoices

        # 7. Invoice Lines (Line items for invoices)
        invoice_line_domain = [
            ('move_id', 'in', [inv['id'] for inv in invoices])
        ]
        invoice_lines = request.env['account.move.line'].with_user(user).search_read(
            invoice_line_domain,
            ['id', 'move_id', 'product_id', 'name', 'quantity', 'price_unit', 'price_subtotal']
        )
        downloads['invoice_lines'] = invoice_lines

        # 8. Payments (Created or modified from last sync date)
        # payment_domain = [
        #     ('state', '=', 'posted'),
        #     ('payment_date', '>=', last_sync_date)
        # ]
        # payments = request.env['account.payment'].with_user(user).search_read(
        #     payment_domain,
        #     ['id', 'name', 'payment_type', 'partner_id', 'payment_date', 'amount', 'journal_id', 'payment_method_line_id', 'ref', 'communication', 'reconciled', 'invoice_ids']
        # )
        # downloads['payments'] = payments

        # 10. Sales Orders
        sales_orders = []
        related_partner_ids = set()  # Initialized here to avoid UnboundLocalError if try block fails
        try:
            sales_order_domain = [
                ('user_id', '=', user.id),
                ('company_id', '=', sales_rep.company_id.id),
            ]
            
            sales_orders = request.env['sale.order'].with_user(user).search_read(
                get_delta_domain(sales_order_domain),
                ['id', 'name', 'partner_id', 'date_order', 'amount_total', 'amount_untaxed', 'amount_tax',
                 'state', 'invoice_status', 'delivery_status', 'pricelist_id', 'route_id', 'visit_id',
                 'order_line', 'mobile_local_id', 'is_cash', 'discount_total']
            )

            # Collect related partner IDs to ensure they exist locally
            # NEW: Also include partners modified since last sync (e.g. credit limit changed)
            if since:
                modified_partners = request.env['res.partner'].with_user(user).search([
                    ('write_date', '>', since),
                    ('company_id', 'in', [sales_rep.company_id.id, False])
                ], limit=100)
                related_partner_ids.update(modified_partners.ids)

            # Collect partner IDs from pickings that will be downloaded
            try:
                picking_domain_for_partners = [
                    '|', '|', '|',
                    ('sales_rep_id', '=', sales_rep.id),
                    ('created_sales_rep_id', '=', sales_rep.id),
                    ('sale_id.sales_rep_id', '=', sales_rep.id),
                    ('partner_id', '!=', False)
                ]
                picking_domain_for_partners = ['&', ('write_date', '>=', thirty_days_ago)] + picking_domain_for_partners
                picking_partners = request.env['stock.picking'].with_user(user).search(picking_domain_for_partners).mapped('partner_id').ids
                related_partner_ids.update(picking_partners)
            except Exception as pe:
                _logger.warning(f"Sync: Failed to pre-fetch picking partner IDs: {pe}")

            # Flatten Many2one fields and add related IDs for mobile consumption
            for so in sales_orders:
                if so.get('pricelist_id') and isinstance(so['pricelist_id'], (list, tuple)):
                    so['pricelist_id'] = so['pricelist_id'][0]
                if so.get('partner_id') and isinstance(so['partner_id'], (list, tuple)):
                    related_partner_ids.add(so['partner_id'][0])
                    so['partner_id'] = so['partner_id'][0]
                if so.get('route_id') and isinstance(so['route_id'], (list, tuple)):
                    so['route_id'] = so['route_id'][0]
                if so.get('visit_id') and isinstance(so['visit_id'], (list, tuple)):
                    so['visit_id'] = so['visit_id'][0]
                
                # Fetch invoice_ids and picking_ids from the actual record
                try:
                    order_obj = request.env['sale.order'].with_user(user).browse(so['id'])
                    order_sudo = order_obj.sudo()
                    so['invoice_ids'] = order_sudo.invoice_ids.ids if order_sudo.invoice_ids else []
                    pickings = order_sudo.picking_ids
                    so['picking_ids'] = pickings.ids if pickings else []
                    
                    # Compute delivery status from app perspective: 
                    # If Odoo says 'pending' or 'partial' but all pickings are done/cancelled, it means no backorder was made (or canceled).
                    has_pending_picking = any(p.state not in ('done', 'cancel') for p in pickings)
                    if so.get('delivery_status') in ('pending', 'partial') and pickings and not has_pending_picking:
                        so['delivery_status'] = 'full'
                    elif not pickings and any(l.qty_delivered > 0 for l in order_sudo.order_line):
                        # Safety check for cases where delivery exists but pickings are missing from access
                        so['delivery_status'] = 'full'
                    
                    # Gap 8: Compute is_returnable (any picking is 'done')
                    has_done_picking = any(p.state == 'done' for p in pickings)
                    so['is_returnable'] = 1 if has_done_picking else 0
                    
                    # Aggregate payment state across ALL posted invoices
                    posted_invs = order_sudo.invoice_ids.filtered(lambda inv: inv.state == 'posted')
                    if posted_invs:
                        total_residual = sum(posted_invs.mapped('amount_residual'))
                        total_amount = sum(posted_invs.mapped('amount_total'))
                        if total_residual <= 0:
                            so['payment_state'] = 'paid'
                        elif total_residual < total_amount:
                            so['payment_state'] = 'partial'
                        else:
                            so['payment_state'] = 'not_paid'
                        so['amount_residual'] = total_residual  # Always set from invoices
                    else:
                        so['amount_residual'] = so.get('amount_total', 0)
                        so['payment_state'] = 'not_paid'
                except Exception as e:
                    _logger.warning(f"Sync: Error enriching SO {so.get('id')}: {e}")
                    so['invoice_ids'] = []
                    so['picking_ids'] = []
                    so['payment_state'] = False


            downloads['sales_orders'] = sales_orders
            _logger.info(f"Sync: Downloaded {len(sales_orders)} sales orders")
            
        except Exception as e:
            _logger.error(f"Sync: Critical error fetching sales orders: {e}")
            downloads['sales_orders'] = []

        # Fetch extra partners if not covered by routes (simplified check)
        # We can just send them; upsert will handle duplicates.
        if related_partner_ids:
             # Build field list — include financial limit fields if the module is installed
             extra_partner_fields = ['id', 'name', 'phone', 'mobile', 'email', 'street', 'city', 'state_id', 'country_id', 'area', 'vat', 
                 'property_payment_term_id', 'property_product_pricelist', 'credit_limit', 'mobile_local_id', 'visit_latitude', 'visit_longitude', 'is_cash', 'category_id', 'enable_location', 'location_radius']
             has_total_due_field_extra = 'total_due' in request.env['res.partner']._fields
             if has_total_due_field_extra:
                 extra_partner_fields.append('total_due')
             if 'sale_credit_limit' in request.env['res.partner']._fields:
                 extra_partner_fields += ['sale_credit_limit', 'allow_over_sale_credit', 'sale_credit_used']
             partners = request.env['res.partner'].with_user(user).search_read(
                [('id', 'in', list(related_partner_ids))],
                extra_partner_fields
             )
             # Flatten partner fields
             extra_due = request.env['res.partner']._sales_rep_due_map([p['id'] for p in partners], sales_rep.company_id.id)
             for p in partners:
                 if p.get('country_id'): p['country_id'] = p['country_id'][0]
                 if p.get('state_id'): p['state_id'] = p['state_id'][0]
                 if p.get('property_payment_term_id'): p['property_payment_term_id'] = p['property_payment_term_id'][0]
                 if p.get('property_product_pricelist'): p['property_product_pricelist'] = p['property_product_pricelist'][0]
                 p['total_due'] = extra_due.get(p['id'], 0.0)
             
             downloads['partners'] = partners

        # 11. Sales Order Lines (Line items for sales orders)
        sales_order_line_domain = [
            ('order_id', 'in', [order['id'] for order in sales_orders])
        ]
        # Use sudo() for lines to bypass public auth ACL friction on subsidiary models
        sales_order_lines = request.env['sale.order.line'].sudo().search_read(
            sales_order_line_domain,
            ['id', 'order_id', 'product_id', 'name', 'product_uom_qty', 'qty_delivered', 'qty_invoiced',
             'price_unit', 'price_subtotal', 'product_uom', 'is_reward_line', 'location_id']
        )
        
        # Enrich lines with UoM name and Location
        for line in sales_order_lines:
            if line.get('product_uom') and isinstance(line['product_uom'], (list, tuple)):
                line['product_uom_name'] = line['product_uom'][1]
                line['product_uom'] = line['product_uom'][0]
            else:
                line['product_uom_name'] = 'Unit'
                line['product_uom'] = line.get('product_uom') or False

            if line.get('location_id') and isinstance(line['location_id'], (list, tuple)):
                line['location_name'] = line['location_id'][1]
                line['location_id'] = line['location_id'][0]
            else:
                line['location_name'] = False
                line['location_id'] = line.get('location_id') or False
        downloads['sales_order_lines'] = sales_order_lines

        # 12. Payment Journals (Filtered by Sales Rep Assignment)
        try:
            # 12a. Identify assigned payment methods and journals
            assigned_methods = sales_rep.sudo().payment_method_ids
            _logger.info(f"Sync: Rep {sales_rep.name} (ID: {sales_rep.id}) has {len(assigned_methods)} assigned payment methods. Access Cash Balance: {sales_rep.access_cash_balance}")
            
            # Extract journal IDs from assigned methods
            assigned_journal_ids = assigned_methods.mapped('journal_id').ids
            _logger.info(f"Sync: Assigned journal IDs: {assigned_journal_ids}")
            
            journal_domain = [
                ('type', 'in', ['cash', 'bank']),
                ('active', '=', True),
                ('id', 'in', assigned_journal_ids)
            ]
            
            journals = request.env['account.journal'].sudo().search(journal_domain)
            _logger.info(f"Sync: Found {len(journals)} journals for domain {journal_domain}")


            journal_data = []
            # Get active route IDs for balance calculation (last 30 days)
            thirty_days_ago_date = thirty_days_ago.date()
            active_route_ids = request.env['sales.rep.route'].sudo().search([
                ('sales_rep_id', '=', sales_rep.id),
                ('date', '>=', thirty_days_ago_date)
            ]).ids

            for journal in journals:
                try:
                    # In Odoo 18, account.journal does not have a direct 'current_balance' field.
                    # We calculate it by summing the balance of posted move lines on the journal's default account.
                    # This represents the actual GL balance.
                    odoo_balance = 0.0
                    if journal.sudo().default_account_id:
                        move_line_domain = [
                            ('journal_id', '=', journal.id),
                            ('account_id', '=', journal.sudo().default_account_id.id),
                            ('parent_state', '=', 'posted')
                        ]
                        # Use sudo() to ensure balance is accurate regardless of user permissions
                        move_lines = request.env['account.move.line'].sudo().search(move_line_domain)
                        odoo_balance = sum(move_lines.mapped('balance'))
                    
                    _logger.debug(f"Sync: Journal {journal.name} (ID: {journal.id}) calculated balance: {odoo_balance}")
                except Exception as e:
                    _logger.warning(f"Sync: Could not get balance for journal {journal.name}: {e}")
                    odoo_balance = 0.0


                
                journal_data.append({
                    'id': journal.id,
                    'odoo_id': journal.id,
                    'name': journal.name,
                    'type': journal.type,
                    'balance': odoo_balance
                })
            downloads['payment_journals'] = journal_data
            _logger.info(f"Sync: Downloaded {len(journal_data)} payment journals for rep {sales_rep.id}")
        except Exception as e:
            _logger.error(f"Sync: Critical error in payment journals section: {e}", exc_info=True)


        # 13. Pricelists (Active pricelists)
        try:
            # Use sudo() as sales reps might not have explicit read access to pricelists
            if sales_rep.available_pricelist_ids:
                pricelist_domain = [
                    ('id', 'in', sales_rep.available_pricelist_ids.ids)
                ]
            else:
                pricelist_domain = [
                    ('active', '=', True),
                    ('company_id', 'in', [False, sales_rep.company_id.id])
                ]
            pricelists = request.env['product.pricelist'].sudo().search_read(
                pricelist_domain,
                ['id', 'name', 'is_sales_cash']
            )
            if sales_rep.available_pricelist_ids:
                assigned_ids = sales_rep.available_pricelist_ids.ids
                pricelists.sort(key=lambda x: assigned_ids.index(x['id']))
            downloads['pricelists'] = pricelists
            _logger.info(f"Sync: Downloaded {len(pricelists)} pricelists")

            # 13b. Pricelist Items
            item_domain = [
                ('pricelist_id', 'in', [p['id'] for p in pricelists])
            ]
            # Odoo 17 fields for pricelist item
            pricelist_items = request.env['product.pricelist.item'].sudo().search_read(
                item_domain,
                ['id', 'pricelist_id', 'product_id', 'product_tmpl_id', 'categ_id', 
                 'min_quantity', 'date_start', 'date_end', 'compute_price', 
                 'fixed_price', 'percent_price', 'base_pricelist_id']
            )
            # Flatten Many2one
            for item in pricelist_items:
                if item.get('pricelist_id'): item['pricelist_id'] = item['pricelist_id'][0]
                if item.get('product_id'): item['product_id'] = item['product_id'][0]
                if item.get('product_tmpl_id'): item['product_tmpl_id'] = item['product_tmpl_id'][0]
                if item.get('categ_id'): item['categ_id'] = item['categ_id'][0]
                if item.get('base_pricelist_id'): item['base_pricelist_id'] = item['base_pricelist_id'][0]
            
            downloads['pricelist_items'] = pricelist_items
            _logger.info(f"Sync: Downloaded {len(pricelist_items)} pricelist items")
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch pricelists/items: {e}")

        # 14. Payment Terms (Active payment terms)
        try:
            # Use sudo() as sales reps might not have explicit read access to payment terms
            if sales_rep.available_payment_term_ids:
                term_domain = [
                    ('id', 'in', sales_rep.available_payment_term_ids.ids)
                ]
            else:
                term_domain = [
                    ('active', '=', True),
                    ('company_id', 'in', [False, sales_rep.company_id.id])
                ]
            terms = request.env['account.payment.term'].sudo().search_read(
                term_domain,
                ['id', 'name']
            )
            if sales_rep.available_payment_term_ids:
                assigned_ids = sales_rep.available_payment_term_ids.ids
                terms.sort(key=lambda x: assigned_ids.index(x['id']))
            downloads['payment_terms'] = terms
            _logger.info(f"Sync: Downloaded {len(terms)} payment terms")
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch payment terms: {e}")

        # 15. Payment Methods (Filtered by Sales Rep Assignment)
        try:
            payment_method_domain = [
                ('active', '=', True),
                ('id', 'in', sales_rep.payment_method_ids.ids)
            ]

            payment_methods = request.env['sales.rep.payment.method'].with_user(user).search_read(
                payment_method_domain,
                ['id', 'name', 'journal_id', 'payment_type']
            )
            # Remap fields for mobile
            for pm in payment_methods:
                if pm.get('payment_type'):
                    pm['type'] = pm['payment_type']
            downloads['payment_methods'] = payment_methods
            _logger.info(f"Sync: Downloaded {len(payment_methods)} payment methods")
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch payment methods: {e}")
            downloads['payment_methods'] = []

        # 16. Return Reasons
        try:
            model_name = 'customer.return.reason' if 'customer.return.reason' in request.env else 'sales.rep.return.reason'
            reasons = request.env[model_name].with_user(user).search_read(
                [('active', '=', True)], 
                ['id', 'name', 'sequence'],
                order='sequence, name'
            )
            downloads['return_reasons'] = reasons
            _logger.info(f"Sync: Downloaded {len(reasons)} return reasons")
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch return reasons: {e}")
            downloads['return_reasons'] = []

        # 17. Partner Categories (Tags)
        try:
            # sudo() for tags as they are global metadata
            tags = request.env['res.partner.category'].sudo().search_read(
                [], 
                ['id', 'name', 'color']
            )
            downloads['partner_categories'] = tags
            _logger.info(f"Sync: Downloaded {len(tags)} partner categories (tags)")
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch partner categories: {e}")
            downloads['partner_categories'] = []

        # 18. Units of Measure
        try:
            uom_categories = request.env['uom.category'].sudo().search_read([], ['id', 'name'])
            downloads['uom_categories'] = uom_categories
            
            uoms = request.env['uom.uom'].sudo().search_read(
                [('active', '=', True)], 
                ['id', 'name', 'category_id', 'factor', 'uom_type', 'rounding']
            )
            # Flatten category_id
            for u in uoms:
                if u.get('category_id'):
                    u['category_id'] = u['category_id'][0]
            downloads['uoms'] = uoms
            _logger.info(f"Sync: Downloaded {len(uom_categories)} UoM categories and {len(uoms)} UoMs")
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch UoM data: {e}")

        # 19. Stock Requests
        try:
            # We want only pickings that are internal stock requests for this sales rep
            request_domain = [
                ('picking_type_id.code', '=', 'internal'),
                '|',
                ('sales_rep_id', '=', sales_rep.id),
                ('location_dest_id', '=', sales_rep.default_location_id.id)
            ]
            if since:
                request_domain.append(('write_date', '>', since))
            
            pickings = request.env['stock.picking'].with_user(user).search(request_domain)
            requests_list = []
            for picking in pickings:
                # Format request date
                req_date = (picking.date_done or picking.scheduled_date or picking.date or fields.Datetime.now())
                if isinstance(req_date, (fields.Datetime, fields.Date)):
                    req_date = req_date.isoformat()
                elif hasattr(req_date, 'strftime'):
                    req_date = req_date.strftime('%Y-%m-%dT%H:%M:%SZ')
                else:
                    req_date = str(req_date)

                # Format request data to match mobile schema expectation
                req_data = {
                    'id': picking.id,
                    'local_id': picking.origin or f"REQ-{picking.id}", # fallback if origin is empty
                    'name': picking.name,
                    'date': req_date,
                    'state': 'done' if picking.state == 'done' else ('cancel' if picking.state == 'cancel' else 'draft'),
                    'sales_rep_id': sales_rep.id,
                    'location_id': picking.location_dest_id.id,
                    'source_location_id': picking.location_id.id,
                    'location_name': picking.location_dest_id.display_name or picking.location_dest_id.name,
                    'source_location_name': picking.location_id.display_name or picking.location_id.name,
                    'lines': []
                }
                
                for move in picking.move_ids:
                    req_data['lines'].append({
                        'id': move.id,
                        'product_id': move.product_id.id,
                        'qty': move.product_uom_qty,
                        'uom_id': move.product_uom.id
                    })
                requests_list.append(req_data)
                
            downloads['stock_requests'] = requests_list
            _logger.info(f"Sync: Downloaded {len(requests_list)} stock requests")
        except Exception as e:
            import traceback
            _logger.warning(f"Sync: Could not fetch stock requests: {e}\n{traceback.format_exc()}")

        # 20. All Stock Pickings & Moves (Deliveries)
        try:
            # Fetch all pickings of the last 30 days to ensure offline database tables are fully populated
            picking_domain = [
                '|', '|',
                ('sales_rep_id', '=', sales_rep.id),
                ('created_sales_rep_id', '=', sales_rep.id),
                ('sale_id.sales_rep_id', '=', sales_rep.id)
            ]
            picking_domain = ['&', ('write_date', '>=', thirty_days_ago)] + picking_domain
            
            all_pickings = request.env['stock.picking'].with_user(user).search(picking_domain)
            pickings_list = []
            moves_list = []
            
            for picking in all_pickings:
                pick_date = (picking.date_done or picking.scheduled_date or picking.date or fields.Datetime.now())
                if hasattr(pick_date, 'strftime'):
                    pick_date_str = pick_date.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    pick_date_str = str(pick_date)
                    
                pickings_list.append({
                    'id': picking.id,
                    'name': picking.name,
                    'state': picking.state,
                    'location_name': picking.location_id.display_name or picking.location_id.name,
                    'location_dest_name': picking.location_dest_id.display_name or picking.location_dest_id.name,
                    'date': pick_date_str,
                    'origin': picking.origin or '',
                    'partner_id': picking.partner_id.id if picking.partner_id else False,
                    'partner_name': picking.partner_id.name if picking.partner_id else '',
                    'picking_type_code': picking.picking_type_code or ''
                })
                
                for move in picking.move_ids:
                    moves_list.append({
                        'id': move.id,
                        'picking_id': picking.id,
                        'product_id': move.product_id.id,
                        'product_name': move.product_id.display_name or move.product_id.name,
                        'product_uom_qty': move.product_uom_qty,
                        'quantity': move.quantity,
                        'state': move.state,
                        'product_uom': move.product_uom.name or ''
                    })
                    
            downloads['stock_pickings'] = pickings_list
            downloads['stock_moves'] = moves_list
            _logger.info(f"Sync: Downloaded {len(pickings_list)} pickings and {len(moves_list)} moves")
        except Exception as e:
            import traceback
            _logger.warning(f"Sync: Could not fetch stock pickings: {e}\n{traceback.format_exc()}")

        # 21. Product Stock levels for Sales Rep Locations
        try:
            location_ids = set()
            if sales_rep.default_location_id:
                location_ids.add(sales_rep.default_location_id.id)
            if sales_rep.default_location_ids:
                for loc in sales_rep.default_location_ids:
                    location_ids.add(loc.id)
            if sales_rep.return_location_id:
                location_ids.add(sales_rep.return_location_id.id)
            if sales_rep.return_location_ids:
                for loc in sales_rep.return_location_ids:
                    location_ids.add(loc.id)
            if hasattr(sales_rep, 'location_request_id') and sales_rep.location_request_id:
                location_ids.add(sales_rep.location_request_id.id)
            if hasattr(sales_rep, 'location_request_ids') and sales_rep.location_request_ids:
                for loc in sales_rep.location_request_ids:
                    location_ids.add(loc.id)

            if location_ids:
                quants = request.env['stock.quant'].sudo().search([
                    ('location_id', 'in', list(location_ids)),
                    ('product_id.active', '=', True)
                ])
                stock_list = []
                for q in quants:
                    stock_list.append({
                        'product_id': q.product_id.id,
                        'location_id': q.location_id.id,
                        'sales_rep_id': sales_rep.id,
                        'quantity': q.quantity or 0.0
                    })
                downloads['product_stock'] = stock_list
                _logger.info(f"Sync: Downloaded {len(stock_list)} product stock records for locations: {list(location_ids)}")
            else:
                downloads['product_stock'] = []
        except Exception as e:
            _logger.warning(f"Sync: Could not fetch product stock: {e}")

        # Debt snapshot for every customer the rep works with. Sent on EVERY
        # sync (not only when the partner record changed): a payment or an
        # invoice posted in Odoo never touches res.partner.write_date, so the
        # delta partner download alone leaves the local balance stale.
        try:
            scope = self._rep_partner_scope(sales_rep, partner_ids)
            due = request.env['res.partner']._sales_rep_due_map(scope, sales_rep.company_id.id)
            downloads['partner_debts'] = [{'id': pid, 'total_due': amount} for pid, amount in due.items()]
            # full snapshot every sync: the app replaces its local list
            downloads['open_invoices'] = self._rep_open_invoices(
                sales_rep, [pid for pid, amount in due.items() if amount > 0])
        except Exception as e:
            _logger.warning(f"Sync: partner debt snapshot failed: {e}")

        return downloads
