# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Implementation of azure interface.

This only implements the requires side, currently, since the providers
is still using the Reactive Charm framework self.
"""
import json
from hashlib import sha256
import logging
import ops
import os
from functools import cached_property
from typing import Mapping, Optional
from urllib.request import urlopen, Request


log = logging.getLogger(__name__)

# block size to read data from Azure metadata service
# (realistically, just needs to be bigger than ~20 chars)
READ_BLOCK_SIZE = 2048

# https://docs.microsoft.com/en-us/azure/virtual-machines/windows/instance-metadata-service
METADATA_URL = "http://169.254.169.254/metadata/instance?api-version=2017-12-01"  # noqa
METADATA_HEADERS = {"Metadata": "true"}


def _request(url):
    req = Request(url, headers=METADATA_HEADERS)
    with urlopen(req) as fd:
        return fd.read(READ_BLOCK_SIZE).decode("utf8").strip()


class AzureIntegrationRequires(ops.Object):
    """

    Interface to request integration access.

    Note that due to resource limits and permissions granularity, policies are
    limited to being applied at the charm level.  That means that, if any
    permissions are requested (i.e., any of the enable methods are called),
    what is granted will be the sum of those ever requested by any instance of
    the charm on this cloud.

    Labels, on the other hand, will be instance specific.

    Example usage:

    ```python

    class MyCharm(ops.CharmBase):

        def __init__(self, *args):
            super().__init__(*args)
            self.azure = AzureIntegrationRequires(self)
            ...

        def request_azure_integration():
            self.azure.request_instance_tags({
                'tag1': 'value1',
                'tag2': None,
            })
            azure.request_load_balancer_management()
            # ...

        def check_azure_integration():
            if self.azure.is_ready():
                update_config_enable_azure()
        ```
    """

    _stored = ops.StoredState()

    def __init__(self, charm: ops.CharmBase, endpoint="azure"):
        super().__init__(charm, f"relation-{endpoint}")
        self.endpoint = endpoint
        self.charm = charm

        events = charm.on[endpoint]
        self.framework.observe(events.relation_joined, self.send_instance_info)
        self._stored.set_default(vm_metadata=None)

    @property
    def relation(self) -> Optional[ops.Relation]:
        """The relation to the integrator, or None."""
        relations = self.charm.model.relations.get(self.endpoint)
        return relations[0] if relations else None

    @property
    def _received(self) -> Mapping[str, str]:
        """
        Helper to streamline access to received data since we expect to only
        ever be connected to a single Azure integration application with a
        single unit.
        """
        if self.relation and self.relation.units:
            return self.relation.data[list(self.relation.units)[0]]
        return {}

    @property
    def _to_publish(self):
        """
        Helper to streamline access to received data since we expect to only
        ever be connected to a single Azure integration application with a
        single unit.
        """
        if self.relation:
            return self.relation.data[self.charm.model.unit]
        return {}

    def send_instance_info(self, _):
        info = {
            "charm": self.charm.meta.name,
            "vm-id": self.vm_id,
            "vm-name": self.vm_name,
            "vm-location": self.vm_location,
            "res-group": self.resource_group,
            "subscription-id": self.subscription_id,
            "model-uuid": os.environ["JUJU_MODEL_UUID"],
        }
        log.info(
            "%s is vm_id=%s (vm_name=%s) in vm-location=%s",
            self.charm.unit.name,
            self.vm_id,
            self.vm_name,
            self.vm_location,
        )
        self._request(info)

    @cached_property
    def vm_metadata(self):
        """This unit's metadata."""
        if self._stored.vm_metadata is None:
            self._stored.vm_metadata = json.loads(_request(METADATA_URL))
        return self._stored.vm_metadata

    @property
    def vm_id(self):
        """This unit's instance-id."""
        return self.vm_metadata["compute"]["vmId"]

    @property
    def vm_name(self):
        """
        This unit's instance name.
        """
        return self.vm_metadata["compute"]["name"]

    @property
    def vm_location(self):
        """
        The location (region) the instance is running in.
        """
        return self.vm_metadata["compute"]["location"]

    @property
    def resource_group(self):
        """
        The resource group this unit is in.
        """
        return self.vm_metadata["compute"]["resourceGroupName"]

    @property
    def subscription_id(self):
        """
        The ID of the Azure Subscription this unit is in.
        """
        return self.vm_metadata["compute"]["subscriptionId"]

    @property
    def resource_group_location(self):
        """
        The location (region) the resource group is in.
        """
        return self._received["resource-group-location"]

    @property
    def vnet_name(self):
        """
        The name of the virtual network the instance is in.
        """
        return self._received["vnet-name"]

    @property
    def vnet_resource_group(self):
        """
        The name of the virtual network the instance is in.
        """
        return self._received["vnet-resource-group"]

    @property
    def subnet_name(self):
        """
        The name of the subnet the instance is in.
        """
        return self._received["subnet-name"]

    @property
    def security_group_name(self):
        """
        The name of the security group attached to the cluster's subnet.
        """
        return self._received["security-group-name"]

    @property
    def security_group_resource_group(self):
        return self._received["security-group-resource-group"]

    @property
    def managed_identity(self):
        return self._received["use-managed-identity"]

    @property
    def aad_client_id(self):
        return self._received["aad-client"]

    @property
    def aad_client_secret(self):
        return self._received["aad-client-secret"]

    @property
    def tenant_id(self):
        return self._received["tenant-id"]

    @property
    def is_ready(self):
        """
        Whether or not the request for this instance has been completed.
        """
        requested = self._to_publish.get("requested")
        completed = json.loads(self._received.get("completed", "{}")).get(self.vm_id)
        ready = bool(requested and requested == completed)
        if not requested:
            log.warning("Local end has yet to request integration")
        if not completed:
            log.warning("Remote end has yet to calculate a response")
        elif not ready:
            log.warning(
                "Waiting for completed=%s to be requested=%s", completed, requested
            )
        return ready

    def evaluate_relation(self, event) -> Optional[str]:
        """Determine if relation is ready."""
        no_relation = not self.relation or (
            isinstance(event, ops.RelationBrokenEvent)
            and event.relation is self.relation
        )
        if no_relation:
            return f"Missing required {self.endpoint}"
        if not self.is_ready:
            return f"Waiting for {self.endpoint}"
        return None

    @property
    def _expected_hash(self):
        def from_json(s: str):
            try:
                return json.loads(s)
            except json.decoder.JSONDecodeError:
                return s

        to_sha = {key: from_json(val) for key, val in self._to_publish.items()}
        return sha256(json.dumps(to_sha, sort_keys=True).encode()).hexdigest()

    def _request(self, keyvals):
        kwds = {key: json.dumps(val) for key, val in keyvals.items()}
        self._to_publish.update(**kwds)
        self._to_publish["requested"] = self._expected_hash

    def tag_instance(self, tags):
        """
        Request that the given tags be applied to this instance.

        # Parameters
        `tags` (dict): Mapping of tag names to values (or `None`).
        """
        self._request({"instance-tags": dict(tags)})

    """Alias for tag_instance"""

    def enable_instance_inspection(self):
        """
        Request the ability to inspect instances.
        """
        self._request({"enable-instance-inspection": True})

    def enable_network_management(self):
        """
        Request the ability to manage networking.
        """
        self._request({"enable-network-management": True})

    def enable_loadbalancer_management(self):
        """
        Request the ability to manage networking.
        """
        self._request({"enable-loadbalancer-management": True})

    def enable_security_management(self):
        """
        Request the ability to manage security (e.g., firewalls).
        """
        self._request({"enable-security-management": True})

    def enable_block_storage_management(self):
        """
        Request the ability to manage block storage.
        """
        self._request({"enable-block-storage-management": True})

    def enable_dns_management(self):
        """
        Request the ability to manage DNS.
        """
        self._request({"enable-dns": True})

    def enable_object_storage_access(self):
        """
        Request the ability to access object storage.
        """
        self._request({"enable-object-storage-access": True})

    def enable_object_storage_management(self):
        """
        Request the ability to manage object storage.
        """
        self._request({"enable-object-storage-management": True})
