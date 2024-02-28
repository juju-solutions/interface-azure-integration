# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
import io
import json
import unittest.mock as mock
from pathlib import Path

import pytest
import yaml
import ops
import ops.testing
from ops.interface_azure.requires import AzureIntegrationRequires, METADATA_URL
import os


class MyCharm(ops.CharmBase):
    azure_meta = ops.RelationMeta(
        ops.RelationRole.requires, "azure", {"interface": "azure-integration"}
    )

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.azure = AzureIntegrationRequires(self)


@pytest.fixture(autouse=True)
def juju_enviro():
    with mock.patch.dict(
        os.environ, {"JUJU_MODEL_UUID": "cf67b90e-7201-4f23-8c0a-e1f453f1dc2e"}
    ):
        yield


@pytest.fixture(scope="function")
def harness():
    harness = ops.testing.Harness(MyCharm)
    harness.framework.meta.name = "test"
    harness.framework.meta.relations = {
        MyCharm.azure_meta.relation_name: MyCharm.azure_meta
    }
    harness.set_model_name("test/0")
    harness.begin_with_initial_hooks()
    yield harness


@pytest.fixture(autouse=True)
def mock_url():
    with mock.patch("ops.interface_azure.requires.urlopen") as urlopen:

        def urlopener(req):
            if req.full_url == METADATA_URL:
                resp = dict(
                    compute=dict(
                        vmId="i-abcdefghijklmnopq",
                        name="n",
                        location="l",
                        resourceGroupName="rgn",
                        subscriptionId="si",
                    )
                )
                return io.BytesIO(json.dumps(resp).encode())

        urlopen.side_effect = urlopener
        yield urlopen


@pytest.fixture()
def integrator_data():
    yield yaml.safe_load(Path("tests/data/from_integrator.yaml").open())


@pytest.fixture()
def sent_data():
    yield yaml.safe_load(Path("tests/data/azure_sent.yaml").open())


@pytest.mark.parametrize(
    "event_type", [None, ops.RelationBrokenEvent], ids=["unrelated", "dropped relation"]
)
def test_is_ready_no_relation(harness, event_type):
    event = ops.ConfigChangedEvent(None)
    assert harness.charm.azure.is_ready is False
    assert "Missing" in harness.charm.azure.evaluate_relation(event)

    rel_id = harness.add_relation("azure", "remote")
    assert harness.charm.azure.is_ready is False

    rel = harness.model.get_relation("azure", rel_id)
    harness.add_relation_unit(rel_id, "remote/0")
    event = ops.RelationJoinedEvent(None, rel)
    assert "Waiting" in harness.charm.azure.evaluate_relation(event)

    event = ops.RelationChangedEvent(None, rel)
    harness.update_relation_data(rel_id, "remote/0", {"completed": "{}"})
    assert "Waiting" in harness.charm.azure.evaluate_relation(event)

    if event_type:
        harness.remove_relation(rel_id)
        event = event_type(None, rel)
        assert "Missing" in harness.charm.azure.evaluate_relation(event)


def test_is_ready_success(harness, integrator_data):
    chksum = json.loads(integrator_data["completed"])["i-abcdefghijklmnopq"]
    completed = '{"i-abcdefghijklmnopq": "%s"}' % chksum
    harness.add_relation("azure", "remote", unit_data={"completed": completed})
    assert harness.charm.azure.is_ready is True
    event = ops.ConfigChangedEvent(None)
    assert harness.charm.azure.evaluate_relation(event) is None


@pytest.mark.parametrize(
    "method_name, args",
    [
        ("tag_instance", 'tags={"tag1": "val1", "tag2": "val2"}'),
        ("enable_instance_inspection", None),
        ("enable_network_management", None),
        ("enable_loadbalancer_management", None),
        ("enable_security_management", None),
        ("enable_block_storage_management", None),
        ("enable_dns_management", None),
        ("enable_object_storage_access", None),
        ("enable_object_storage_management", None),
    ],
)
def test_request_simple(harness, method_name, args, sent_data):
    rel_id = harness.add_relation("azure", "remote")
    method = getattr(harness.charm.azure, method_name)
    kwargs = {}
    if args:
        kw, val = args.split("=")
        kwargs[kw] = json.loads(val)
    method(**kwargs)
    data = harness.get_relation_data(rel_id, harness.charm.unit.name)
    assert data.pop("requested")
    for each, value in data.items():
        assert sent_data[each] == value


@pytest.mark.parametrize(
    "method_name, expected",
    [
        ("resource_group_location", "rgl"),
        ("vnet_name", "vn"),
        ("vnet_resource_group", "vrg"),
        ("subnet_name", "sn"),
        ("security_group_name", "sgn"),
        ("security_group_resource_group", "sgrg"),
        ("managed_identity", "umi"),
        ("aad_client_id", "aci"),
        ("aad_client_secret", "acs"),
        ("tenant_id", "ti"),
    ],
)
def test_received_data(harness, method_name, expected, integrator_data):
    harness.add_relation("azure", "remote", unit_data=integrator_data)
    assert getattr(harness.charm.azure, method_name) == expected
