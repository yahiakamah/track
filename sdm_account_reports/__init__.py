# -*- coding: utf-8 -*-


from . import models
from . import controllers
from . import wizard


def set_periodicity_journal_on_companies(env):
    for company in env['res.company'].search([]):
        company.account_tax_periodicity_journal_id = company.with_company(company)._get_default_misc_journal()
