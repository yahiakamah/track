def uninstall_hook(env):
    env.cr.execute("UPDATE ir_act_window "
               "SET view_mode=replace(view_mode, ',leaflet_map', '')"
               "WHERE view_mode LIKE '%,leaflet_map%';")
    env.cr.execute("UPDATE ir_act_window "
               "SET view_mode=replace(view_mode, 'leaflet_map,', '')"
               "WHERE view_mode LIKE '%leaflet_map,%';")
    env.cr.execute("DELETE FROM ir_act_window "
               "WHERE view_mode = 'leaflet_map';")
