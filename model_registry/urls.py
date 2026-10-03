from django.urls import path

from . import agent_views, views

urlpatterns = [
    path("models/ping-connection/<int:conn_pk>/", views.ping_connection, name="conn-ping"),
    # Agent configuration domain
    path("agents/", agent_views.agents, name="agent-list"),
    path("agents/<int:pk>/", agent_views.agent_detail, name="agent-detail"),
    path("agents/<int:pk>/snapshot/", agent_views.agent_snapshot, name="agent-snapshot"),
    path("retrieval-profiles/", agent_views.retrieval_profiles, name="retrieval-profile-list"),
    path("retrieval-profiles/<int:pk>/", agent_views.retrieval_profile_detail, name="retrieval-profile-detail"),
    path("knowledge-bases/", agent_views.knowledge_bases, name="knowledge-base-list"),
    path("knowledge-bases/<int:pk>/", agent_views.knowledge_base_detail, name="knowledge-base-detail"),
    path("tools/", agent_views.tools, name="tool-list"),
    path("tools/<int:pk>/", agent_views.tool_detail, name="tool-detail"),
]
