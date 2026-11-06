# -*- coding: utf-8 -*-
from odoo import models, fields, api
import io
import xlsxwriter
import base64
from datetime import datetime

class SalesRepSessionReportWizard(models.TransientModel):
    _name = 'sales.rep.session.report.wizard'
    _description = 'Representative Session Report Wizard'

    sales_rep_ids = fields.Many2many(
        'sales.representative',
        string='Sales Representatives'
    )
    from_date = fields.Datetime(
        string='From Date',
        required=True,
        default=fields.Datetime.now
    )
    to_date = fields.Datetime(
        string='To Date',
        required=True,
        default=fields.Datetime.now
    )
    action_filter = fields.Selection([
        ('all', 'All'),
        ('login', 'Login Only'),
        ('logout', 'Logout Only')
    ], string='Action Type', required=True, default='all')
    export_format = fields.Selection([
        ('xlsx', 'Excel'),
        ('pdf', 'PDF')
    ], string='Export Format', required=True, default='xlsx')

    def generate_report(self):
        self.ensure_one()
        if self.export_format == 'xlsx':
            return self._generate_excel_report()
        else:
            return self._generate_pdf_report()

    def _get_report_data(self):
        domain = [
            ('timestamp', '>=', self.from_date),
            ('timestamp', '<=', self.to_date)
        ]
        if self.sales_rep_ids:
            domain.append(('sales_rep_id', 'in', self.sales_rep_ids.ids))
        if self.action_filter != 'all':
            domain.append(('action_type', '=', self.action_filter))
            
        return self.env['sales.representative.session.log'].sudo().search(domain, order='timestamp desc')

    def _generate_pdf_report(self):
        return self.env.ref('sales_rep_management.action_sales_rep_session_pdf_report').report_action(self)

    def get_report_logs_data(self):
        logs = self._get_report_data()
        # Find pairs
        session_pairs = {}
        for log in logs:
            if not log.session_identifier or not log.sales_rep_id:
                continue
            key = (log.sales_rep_id.id, log.session_identifier)
            session_pairs.setdefault(key, []).append(log)
            
        # Now, for each log, determine duration info
        duration_info = {}
        for key, log_list in session_pairs.items():
            login_log = next((l for l in log_list if l.action_type == 'login'), None)
            logout_log = next((l for l in log_list if l.action_type == 'logout'), None)
            
            if login_log and logout_log:
                duration_td = logout_log.timestamp - login_log.timestamp
                total_seconds = int(duration_td.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                seconds = total_seconds % 60
                duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                
                duration_info[logout_log.id] = {
                    'rowspan': 2,
                    'value': duration_str,
                    'skip': False
                }
                duration_info[login_log.id] = {
                    'rowspan': 1,
                    'value': '',
                    'skip': True
                }
            else:
                for l in log_list:
                    duration_info[l.id] = {
                        'rowspan': 1,
                        'value': '',
                        'skip': False
                    }
                    
        return logs, duration_info

    def _generate_excel_report(self):
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)
        worksheet = workbook.add_worksheet('Session Log')

        # Title Formatting
        title_format = workbook.add_format({
            'bold': True,
            'font_size': 14,
            'font_color': '#1E3A8A',
            'align': 'center'
        })
        header_format = workbook.add_format({
            'bold': True,
            'bg_color': '#D3D3D3',
            'border': 1,
            'align': 'center'
        })
        cell_format = workbook.add_format({
            'border': 1,
            'align': 'left'
        })
        date_format = workbook.add_format({
            'border': 1,
            'num_format': 'yyyy-mm-dd hh:mm:ss',
            'align': 'center'
        })
        link_format = workbook.add_format({
            'color': 'blue',
            'underline': 1,
            'border': 1,
            'align': 'center'
        })
        duration_cell_format = workbook.add_format({
            'border': 1,
            'align': 'center',
            'valign': 'vcenter',
            'bold': True
        })

        # Title
        worksheet.merge_range('A1:I1', 'Representative Device Session Report', title_format)
        worksheet.write('A2', f"Period: {self.from_date} to {self.to_date}", workbook.add_format({'italic': True}))
        
        # Headers
        headers = ['Rep Code', 'Representative Name', 'Session Reference', 'Action Type', 'Timestamp', 
                  'Duration', 'Device Model', 'MAC Address', 'View on Map']
                  
        for col_idx, header in enumerate(headers):
            worksheet.write(3, col_idx, header, header_format)

        # Data
        logs, duration_info = self.get_report_logs_data()
        row = 4
        
        # Keep track of written log rows to merge duration cells
        log_rows = {}
        
        for log in logs:
            worksheet.write(row, 0, log.sales_rep_id.code or '', cell_format)
            worksheet.write(row, 1, log.sales_rep_id.name or '', cell_format)
            worksheet.write(row, 2, log.session_sequence or '', cell_format)
            worksheet.write(row, 3, log.action_type.upper(), cell_format)
            
            if log.timestamp:
                worksheet.write_datetime(row, 4, log.timestamp, date_format)
            else:
                worksheet.write(row, 4, '', cell_format)
                
            # Initialize Duration cell with default border format
            worksheet.write(row, 5, '', cell_format)
            
            worksheet.write(row, 6, log.device_model or '', cell_format)
            worksheet.write(row, 7, log.mac_address or '', cell_format)

            if log.latitude and log.longitude:
                url = f"https://www.google.com/maps/search/?api=1&query={log.latitude},{log.longitude}"
                worksheet.write_url(row, 8, url, string='View on Map', cell_format=link_format)
            else:
                worksheet.write(row, 8, '', cell_format)
                
            log_rows[log.id] = row
            row += 1

        # Merge duration cells for paired logins/logouts
        session_pairs = {}
        for log in logs:
            if not log.session_identifier or not log.sales_rep_id:
                continue
            key = (log.sales_rep_id.id, log.session_identifier)
            session_pairs.setdefault(key, []).append(log)

        for key, log_list in session_pairs.items():
            login_log = next((l for l in log_list if l.action_type == 'login'), None)
            logout_log = next((l for l in log_list if l.action_type == 'logout'), None)
            
            if login_log and logout_log:
                r_in = log_rows.get(login_log.id)
                r_out = log_rows.get(logout_log.id)
                
                info = duration_info.get(logout_log.id, {})
                duration_str = info.get('value', '')
                
                if r_in is not None and r_out is not None:
                    if abs(r_in - r_out) == 1:
                        worksheet.merge_range(min(r_in, r_out), 5, max(r_in, r_out), 5, duration_str, duration_cell_format)
                    else:
                        worksheet.write(r_in, 5, duration_str, duration_cell_format)
                        worksheet.write(r_out, 5, duration_str, duration_cell_format)

        worksheet.set_column('A:B', 20)
        worksheet.set_column('C:D', 18)
        worksheet.set_column('E:F', 18)
        worksheet.set_column('G:H', 22)
        worksheet.set_column('I:I', 16)

        workbook.close()
        output.seek(0)

        filename = f"rep_sessions_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(output.read()),
            'res_model': self._name,
            'res_id': self.id,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }
