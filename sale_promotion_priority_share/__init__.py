from . import models


def assign_priorities(env):
    """Post-install hook: assign unique priorities to existing programs."""
    programs = env['loyalty.program'].search([], order='id asc')
    for i, program in enumerate(programs, start=1):
        if not program.priority:
            program.priority = i