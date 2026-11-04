"""大模型对话相关组件。"""

from __future__ import annotations

from .chat import ChatService, split_text
from .client import LLMClient, LLMError
from .context_assembly import ContextAssembler
from .context_builder import ContextBuilder
from .model_gateway import ChatReply, ModelGateway
from .personas import DEFAULT_PERSONA_TEXT, PERSONA_VARIABLES, Persona, PersonaStore, render_template

__all__ = [
    "DEFAULT_PERSONA_TEXT",
    "PERSONA_VARIABLES",
    "ChatReply",
    "ChatService",
    "ContextAssembler",
    "ContextBuilder",
    "ModelGateway",
    "LLMClient",
    "LLMError",
    "Persona",
    "PersonaStore",
    "render_template",
    "split_text",
]
