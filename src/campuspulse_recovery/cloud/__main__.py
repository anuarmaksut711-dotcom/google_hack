"""Cloud-only entrypoint; importing does not acquire credentials or call services."""
import logging
import os


def main():
    try:
        if not os.environ.get('K_SERVICE'):
            raise ValueError('cloud_run_environment_required')
        if os.environ.get('FIRESTORE_EMULATOR_HOST') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
            raise ValueError('cloud_platform_credentials_required')
        from .settings import load_settings
        settings = load_settings(os.environ['CAMPUSPULSE_CLOUD_SETTINGS'])
        role = os.environ['CAMPUSPULSE_ROLE']
        if role not in {'backend', 'agent', 'mcp', 'moodle-sync'}:
            raise ValueError('explicit_cloud_role_required')
        port = int(os.environ.get('PORT', '8080'))
        if not 1 <= port <= 65535:
            raise ValueError('invalid_port')
        if role == 'agent' or (role == 'backend' and settings.moodle is None):
            from ..agent.conversation import require_chat_credentials
            require_chat_credentials(settings.chat)
        from google.cloud.firestore import AsyncClient
        from ..security.resources import ResourcePolicy
        from ..storage.plans import FirestoreDocuments
        from .planning import assignments_resource
        policy = ResourcePolicy(settings.application, environment='cloud',
                                assignments_resource_id=assignments_resource(settings))
        client = AsyncClient(project=policy.config.project_id, database=policy.config.firestore_database)
        documents = FirestoreDocuments(client, project_id=policy.config.project_id,
                                      database_id=policy.config.firestore_database)
        if role == 'backend':
            from .backend import create_backend
            app = create_backend(settings, policy, documents, close=client.close)
        elif role == 'agent':
            from .agent import create_agent
            app = create_agent(settings, policy, documents, close=client.close)
        elif role == 'moodle-sync':
            if settings.moodle is None:
                raise ValueError('moodle_settings_required')
            from .moodle_worker import create_moodle_worker
            app = create_moodle_worker(settings, policy, documents, close=client.close)
        else:
            from .mcp import create_mcp
            app = create_mcp(settings, policy, documents, port=port, close=client.close)
    except Exception:
        raise SystemExit('cloud_startup_configuration_invalid_or_incomplete') from None

    # SDK tracebacks can contain user content or remote HTTP details. Business
    # observations are typed/redacted in Firestore, never raw prompts/tokens.
    logging.disable(logging.CRITICAL)
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=port, access_log=False, log_level='critical',
        proxy_headers=True, forwarded_allow_ips='*', timeout_graceful_shutdown=120)


if __name__ == '__main__':
    main()
