# Copyright (c) 2015 Cisco Systems.  All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from neutron.db import l3_db
from neutron.plugins.ml2 import managers
from neutron.plugins.ml2.plugin import Ml2Plugin
from oslo_utils import importutils

try:
    # Icehouse, Juno
    from neutron.openstack.common import log
except ImportError:
    # Kilo
    from oslo_log import log

try:
    # Icehouse, Juno
    from oslo.config import cfg
except ImportError:
    # Kilo
    from oslo_config import cfg

LOG = log.getLogger(__name__)


# Extend the MechanismManager class so that we have a way to pass floating
# IP update messages into the mechanism driver.
class CalicoMechanismManager(managers.MechanismManager):
    def __init__(self, *args, **kw):
        # Force the ML2 driver to use only the Calico mechanism driver.  This
        # just makes it so fewer things need to be configured in neutron.conf.
        LOG.info("Forcing ML2 mechanism_drivers to 'calico'")
        cfg.CONF.ml2.mechanism_drivers = ['calico']
        super(CalicoMechanismManager, self).__init__(*args, **kw)

    def update_floatingip(self, context):
        """Notify the mechanism driver of a floating IP update."""
        self._call_on_drivers("update_floatingip", context)


# Since we have to do all the work to overwrite MechanismManager, it's fairly
# cheap for us to remove the necessity for the user to configure the
# type_managers and tenant_network_types in neutron.conf.  This basically
# means the user shouldn't need to configure anything in the [ml2] config
# section.
class CalicoTypeManager(managers.TypeManager):
    def __init__(self, *args, **kw):
        LOG.info("Forcing ML2 type_drivers to 'local, flat'")
        cfg.CONF.ml2.type_drivers = ['local', 'flat']
        LOG.info("Forcing ML2 tenant_network_types to 'local")
        cfg.CONF.ml2.tenant_network_types = ['local']
        super(CalicoTypeManager, self).__init__(*args, **kw)


class CalicoCorePlugin(Ml2Plugin, l3_db.L3_NAT_db_mixin):
    def __init__(self):
        # Add the ability to handle floating IPs.
        self._supported_extension_aliases.extend(["router"])

        # The following is an annoying duplication from Ml2Plugin.__init__
        # just so that we can use our own MechanismManager class.

        self.type_manager = CalicoTypeManager()
        self.extension_manager = managers.ExtensionManager()
        self.mechanism_manager = CalicoMechanismManager()

        # Ml2Plugin.__init__ calls super on itself, but we can't do that
        # because it would overwrite self.mechanism_manager with a non-Calico
        # MechanismManager.  So we do a bit of a manual super implementation
        # that just makes sure to skip running __init__ in Ml2Plugin.
        for c in CalicoCorePlugin.__mro__:
            if not c in [CalicoCorePlugin, Ml2Plugin]:
                if getattr(c, '__init__', None):
                    c.__init__(self)

        self.type_manager.initialize()
        self.extension_manager.initialize()
        self.mechanism_manager.initialize()
        self._setup_rpc()
        self.network_scheduler = importutils.import_object(
            cfg.CONF.network_scheduler_driver
        )

        # Added to Ml2Plugin.__init__ in Kilo.
        if getattr(self, 'start_periodic_dhcp_agent_status_check', None):
            self.start_periodic_dhcp_agent_status_check()

        LOG.info("Calico Core Plugin initialization complete")

    # Intercept floating IP associates/disassociates so we can trigger an
    # appropriate endpoint update.
    def _update_floatingip(self, context, id, floatingip):
        old_floatingip, new_floatingip = super(
            CalicoCorePlugin, self)._update_floatingip(context, id, floatingip)

        if new_floatingip['port_id']:
            context.fip_update_port_id = new_floatingip['port_id']
            self.mechanism_manager.update_floatingip(context)
        if old_floatingip['port_id']:
            context.fip_update_port_id = old_floatingip['port_id']
            self.mechanism_manager.update_floatingip(context)

        return old_floatingip, new_floatingip
