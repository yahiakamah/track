from odoo import api, SUPERUSER_ID
import logging

_logger = logging.getLogger(__name__)


def assign_priorities(cr, registry):
    """Assign a unique priority to every existing promotion program during module installation."""
    try:
        env = api.Environment(cr, SUPERUSER_ID, {})

        # Check if the model exists in the registry
        if 'loyalty.program' not in env.registry:
            _logger.warning("loyalty.program model not found in registry yet")
            return

        programs = env['loyalty.program'].search([], order='id')

        if not programs:
            _logger.info("No promotion programs found to assign priorities")
            return

        _logger.info("Assigning priorities to %d promotion programs", len(programs))

        for idx, program in enumerate(programs, start=1):
            # Check if priority is already set and unique
            existing = env['loyalty.program'].search([
                ('priority', '=', idx),
                ('id', '!=', program.id)
            ])
            if existing:
                # Find next available priority
                while env['loyalty.program'].search_count([('priority', '=', idx)]) > 0:
                    idx += 1

            program.write({'priority': idx})

        env.cr.commit()
        _logger.info("Successfully assigned priorities to all promotion programs")

    except Exception as e:
        _logger.error("Error assigning priorities: %s", e)
        pass