"""Verify the pinned SDK's real custom-operation and pickle contracts."""
import cloudpickle

from tests.agent_engine.test_runtime import settings, no_cloud


def test_pickle_rebuilds_from_settings_and_drops_transient_runtime_state():
    from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
    runtime = AgentEngineRuntime(settings())
    runtime._future_token_cache = 'transient-token-never-serialize'
    runtime._future_client = lambda: 'transient-client-never-serialize'
    data = cloudpickle.dumps(runtime)
    assert b'transient-token-never-serialize' not in data
    assert b'transient-client-never-serialize' not in data
    restored = cloudpickle.loads(data)
    assert type(restored) is AgentEngineRuntime
    assert restored._settings_json == runtime._settings_json
    assert set(vars(restored)) == {'_settings_json'}


def test_pinned_vertex_sdk_can_generate_only_the_private_async_query_schema():
    from vertexai.agent_engines import _agent_engines as sdk
    from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
    runtime = AgentEngineRuntime(settings())
    sdk._validate_agent_engine_or_raise(runtime)
    schemas = sdk._generate_class_methods_spec_or_raise(
        agent_engine=runtime, operations=runtime.register_operations())
    assert len(schemas) == 1
    spec = schemas[0]
    assert spec['name'] == 'async_query' and spec['api_mode'] == 'async'
    parameters = spec['parameters']
    assert set(parameters['properties']) == {'invocation', 'service_token'}
    assert set(parameters['required']) == {'invocation', 'service_token'}
    assert all(value['type'] == 'string' for value in parameters['properties'].values())
