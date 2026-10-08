from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools import agent_tool, url_context, VertexAiSearchTool
from google.adk.tools.google_search_tool import GoogleSearchTool

# --- AGENTE REGULATORIO ---
agente_regulatorio_google_search_agent = LlmAgent(
    name='Agente_Regulatorio_google_search_agent',
    model='gemini-3.8-flash',
    description='Agent specialized in performing Google searches.',
    sub_agents=[],
    instruction='Use the GoogleSearchTool to find information on the web.',
    tools=[GoogleSearchTool()],
)

agente_regulatorio_url_context_agent = LlmAgent(
    name='Agente_Regulatorio_url_context_agent',
    model='gemini-3.8-flash',
    description='Agent specialized in fetching content from URLs.',
    sub_agents=[],
    instruction='Use the UrlContextTool to retrieve content from provided URLs.',
    tools=[url_context],
)

agente_regulatorio_2 = LlmAgent(
    name='agente_regulatorio_2',
    model='gemini-3.8-flash',
    description='Eres un Analista Regulatorio Senior...',
    sub_agents=[],
    instruction='Analiza el impacto regulatorio en procesos...',
    tools=[
        agent_tool.AgentTool(agent=agente_regulatorio_google_search_agent),
        agent_tool.AgentTool(agent=agente_regulatorio_url_context_agent),
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url='https://mcp-navegar-web-86171080709.us-west1.run.app/mcp',
            ),
        )
    ],
)

# --- AGENTE DESARROLLO DE NEGOCIOS ---
agente_de_desarrollo_de_negocios_google_search_agent = LlmAgent(
    name='Agente_de_Desarrollo_de_Negocios_google_search_agent',
    model='gemini-3.8-flash',
    description='Agent specialized in performing Google searches.',
    sub_agents=[],
    instruction='Use the GoogleSearchTool to find information on the web.',
    tools=[GoogleSearchTool()],
)

agente_de_desarrollo_de_negocios_url_context_agent = LlmAgent(
    name='Agente_de_Desarrollo_de_Negocios_url_context_agent',
    model='gemini-3.8-flash',
    description='Agent specialized in fetching content from URLs.',
    sub_agents=[],
    instruction='Use the UrlContextTool to retrieve content from provided URLs.',
    tools=[url_context],
)

agente_de_desarrollo_de_negocios = LlmAgent(
    name='agente_de_desarrollo_de_negocios',
    model='gemini-3.8-flash',
    description='Eres un analista desarrollador de negocios...',
    sub_agents=[],
    instruction='Exploras fuentes oficiales de oportunidades...',
    tools=[
        agent_tool.AgentTool(agent=agente_de_desarrollo_de_negocios_google_search_agent),
        agent_tool.AgentTool(agent=agente_de_desarrollo_de_negocios_url_context_agent),
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url='https://mcp-navegar-web-86171080709.us-west1.run.app/mcp',
            ),
        )
    ],
)

# --- AGENTE DE ACTUALIDAD ---
agente_de_actualidad_google_search_agent = LlmAgent(
    name='Agente_de_Actualidad_google_search_agent',
    model='gemini-3.8-flash',
    description='Agent specialized in performing Google searches.',
    sub_agents=[],
    instruction='Use the GoogleSearchTool to find information on the web.',
    tools=[GoogleSearchTool()],
)

agente_de_actualidad_url_context_agent = LlmAgent(
    name='Agente_de_Actualidad_url_context_agent',
    model='gemini-3.8-flash',
    description='Agent specialized in fetching content from URLs.',
    sub_agents=[],
    instruction='Use the UrlContextTool to retrieve content from provided URLs.',
    tools=[url_context],
)

agente_de_actualidad = LlmAgent(
    name='agente_de_actualidad',
    model='gemini-3.8-flash',
    description='Eres un especialista energético senior...',
    sub_agents=[],
    instruction='Identificas novedades y evalúas impacto...',
    tools=[
        agent_tool.AgentTool(agent=agente_de_actualidad_google_search_agent),
        agent_tool.AgentTool(agent=agente_de_actualidad_url_context_agent),
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url='https://mcp-navegar-web-86171080709.us-west1.run.app/mcp',
            ),
        )
    ],
)

# --- AGENTE ESTRATÉGICO (ROOT) ---
agente_estrat_gico_url_context_agent = LlmAgent(
    name='Agente_Estrat_gico_url_context_agent',
    model='gemini-3.1-pro-preview',
    description='Agent specialized in fetching content from URLs.',
    sub_agents=[],
    instruction='Use the UrlContextTool to retrieve content from provided URLs.',
    tools=[url_context],
)

agente_estrat_gico_google_search_agent = LlmAgent(
    name='Agente_Estrat_gico_google_search_agent',
    model='gemini-3.1-pro-preview',
    description='Agent specialized in performing Google searches.',
    sub_agents=[],
    instruction='Use the GoogleSearchTool to find information on the web.',
    tools=[GoogleSearchTool()],
)

agente_estrat_gico_vertex_ai_search_agent = LlmAgent(
    name='Agente_Estrat_gico_vertex_ai_search_agent',
    model='gemini-3.1-pro-preview',
    description='Agent specialized in performing Vertex AI Search.',
    sub_agents=[],
    instruction='Use the VertexAISearchTool to find information using Vertex AI Search.',
    tools=[
        VertexAiSearchTool(
            data_store_id='projects/automatizaciones-ia-20260420/locations/us/collections/default_collection/dataStores/okf-sector-energia_1786412630535',
        )
    ],
)

root_agent = LlmAgent(
    name='Agente_Estrat_gico',
    model='gemini-3.1-pro-preview',
    description='Eres el Especialista de Estrategia y Consultor Senior...',
    sub_agents=[agente_regulatorio_2, agente_de_desarrollo_de_negocios, agente_de_actualidad],
    instruction='Director de Estrategia del mercado eléctrico colombiano...',
    tools=[
        agent_tool.AgentTool(agent=agente_estrat_gico_url_context_agent),
        agent_tool.AgentTool(agent=agente_estrat_gico_google_search_agent),
        agent_tool.AgentTool(agent=agente_estrat_gico_vertex_ai_search_agent)
    ],
)