# PrusaKLoad material section loader ([prusa_kload_material <NAME>])
#
# Copyright (C) 2026 PrusaKLoad contributors
#
# This file may be distributed under the terms of the GNU GPLv3 license.


def load_config_prefix(config):
    printer = config.get_printer()
    kload = printer.lookup_object("prusa_kload", None)
    if kload is None:
        kload = printer.load_object(config, "prusa_kload")
    return kload.register_material_config(config)

