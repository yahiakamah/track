# -*- coding: utf-8 -*-
"""Excel export of the field activity report (summary + one sheet per section)."""
import io

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from odoo import _, http
from odoo.http import content_disposition, request

HEAD_FILL = PatternFill('solid', fgColor='4F46E5')
SUB_FILL = PatternFill('solid', fgColor='E0E7FF')
THIN = Side(style='thin', color='CBD5E1')
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
NUM_FMT = {'int': '#,##0', 'float': '#,##0.00', 'money': '#,##0.00', 'pct': '0.0" %"'}


def _header(ws, row, values, fill=HEAD_FILL, color='FFFFFF'):
    for col, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = Font(bold=True, color=color)
        cell.fill = fill
        cell.border = BORDER
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)


def _row(ws, row, values, kinds=None, bold=False):
    for col, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.border = BORDER
        if bold:
            cell.font = Font(bold=True)
        kind = kinds[col - 1] if kinds and col - 1 < len(kinds) else None
        if kind:
            cell.number_format = NUM_FMT[kind]


def _widths(ws, first=28, other=16):
    for col in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(col)].width = first if col == 1 else other


class FieldReportExport(http.Controller):

    @http.route('/sales_rep_management/field_report_excel/<int:wizard_id>', type='http', auth='user')
    def field_report_excel(self, wizard_id, **kw):
        wizard = request.env['sales.rep.field.report.wizard'].browse(wizard_id).exists()
        if not wizard:
            return request.not_found()
        data = wizard.get_report_data(with_details=True, limit=50000)
        rtl = request.env.lang and request.env.lang.startswith('ar')

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = _('Summary')[:31]
        ws.sheet_view.rightToLeft = bool(rtl)
        ws.cell(row=1, column=1, value=_('Field Activity Report')).font = Font(bold=True, size=15)
        r = 3
        for label, value in data['filters']:
            ws.cell(row=r, column=1, value=label).font = Font(bold=True)
            ws.cell(row=r, column=2, value=value)
            r += 1
        r += 1
        for s in data['sections']:
            ws.cell(row=r, column=1, value=s['title']).font = Font(bold=True, size=12, color='4F46E5')
            r += 1
            labels = [m[1] for m in s['measures']] + [k[0] for k in s['kpis']]
            _header(ws, r, labels)
            r += 1
            values = [s['totals'][m[0]] for m in s['measures']] + [k[1] for k in s['kpis']]
            kinds = [m[2] for m in s['measures']] + [k[2] for k in s['kpis']]
            _row(ws, r, values, kinds, bold=True)
            r += 2
        _widths(ws, 22, 16)

        for s in data['sections']:
            sh = wb.create_sheet(s['title'][:31])
            sh.sheet_view.rightToLeft = bool(rtl)
            kinds = [m[2] for m in s['measures']]
            r = 1
            sh.cell(row=r, column=1, value=f"{s['title']} — {data['group_label']}").font = Font(bold=True, size=13)
            r += 2
            _header(sh, r, [data['group_label']] + [m[1] for m in s['measures']])
            r += 1
            for g in s['groups']:
                _row(sh, r, [g['label']] + g['values'], [None] + kinds)
                r += 1
            _row(sh, r, [_('Total')] + [s['totals'][m[0]] for m in s['measures']], [None] + kinds, bold=True)
            sh.freeze_panes = 'B4'
            r += 2
            bd = s['breakdown']
            if bd['rows']:
                sh.cell(row=r, column=1, value=bd['title']).font = Font(bold=True, size=12)
                r += 1
                _header(sh, r, [''] + [m[1] for m in bd['measures']], SUB_FILL, '1E293B')
                r += 1
                for g in bd['rows']:
                    _row(sh, r, [g['label']] + g['values'], [None] + [m[2] for m in bd['measures']])
                    r += 1
                r += 1
            if s['details']:
                sh.cell(row=r, column=1, value=_('Details')).font = Font(bold=True, size=12)
                r += 1
                _header(sh, r, s['detail_headers'], SUB_FILL, '1E293B')
                first_detail = r
                r += 1
                for line in s['details']:
                    _row(sh, r, [self._num(v) for v in line])
                    r += 1
                sh.auto_filter.ref = f"A{first_detail}:{get_column_letter(len(s['detail_headers']))}{r - 1}"
            _widths(sh)

        out = io.BytesIO()
        wb.save(out)
        name = f'field_activity_{wizard.date_from}_{wizard.date_to}.xlsx'
        return request.make_response(out.getvalue(), headers=[
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition(name)),
        ])

    @staticmethod
    def _num(value):
        """Detail cells are pre-formatted strings; give Excel real numbers back."""
        if isinstance(value, str):
            raw = value.replace(',', '')
            try:
                return float(raw) if raw and raw.replace('.', '', 1).lstrip('-').isdigit() else value
            except ValueError:
                return value
        return value
