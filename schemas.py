"""
schemas.py – Pydantic v2 schemas for request validation and response serialization.

Each domain (Node, Port, Settings) has three schema variants:
  - <Model>Base    – shared fields (no id, no computed fields)
  - <Model>Create  – fields accepted on POST (may add write-only fields)
  - <Model>        – full response schema (includes id and read-only fields)
"""

from __future__ import annotations

from typing import Literal, Optional, List
from pydantic import BaseModel, Field, ConfigDict


# ===========================================================================
# Node Schemas
# ===========================================================================

class NodeBase(BaseModel):
    name:     str = Field(..., max_length=255, examples=["US-West-01"])
    protocol: str = Field("vless", max_length=50, examples=["vless"])
    address:  str = Field(..., max_length=255, examples=["example.com"])
    port:     int = Field(..., ge=1, le=65535, examples=[443])
    uuid:     str = Field(..., min_length=1, max_length=36, examples=["abc123"])
    network:  str = Field("tcp", max_length=50, examples=["ws"])
    security: str = Field("none", max_length=50, examples=["tls"])
    raw_link: Optional[str] = Field(None, examples=["vless://..."])
    latency:  Optional[float] = Field(None, examples=[120.5])


class NodeCreate(NodeBase):
    """Payload accepted by POST /api/nodes."""
    pass


class NodeResponse(NodeBase):
    """Full node representation returned in API responses."""
    id: int

    model_config = ConfigDict(from_attributes=True)


# ===========================================================================
# Port Schemas
# ===========================================================================

class PortBase(BaseModel):
    local_port:  int = Field(..., ge=1, le=65535, examples=[10808])
    protocol:    Literal["http", "socks"] = Field("socks", examples=["socks"])
    node_id:     int = Field(..., ge=1, examples=[1])
    auto_assign: bool = Field(False, examples=[False])


class PortCreate(PortBase):
    """Payload accepted by POST /api/ports."""
    pass


class PortResponse(PortBase):
    """Full port representation returned in API responses."""
    id:     int
    status: Literal["running", "stopped"]

    model_config = ConfigDict(from_attributes=True)


# ===========================================================================
# Settings Schemas
# ===========================================================================

class SettingsBase(BaseModel):
    xray_path:       str = Field("./xray", max_length=512, examples=["./xray"])
    routing_mode:    Literal["global", "rule", "direct"] = Field("global")
    enable_sniffing: bool = Field(True)
    enable_mux:      bool = Field(False)
    update_interval: int  = Field(300, ge=10, le=86400, examples=[300])


class SettingsUpdate(SettingsBase):
    """Payload accepted by PUT /api/settings (all fields optional for partial update)."""
    xray_path:       Optional[str]   = None
    routing_mode:    Optional[Literal["global", "rule", "direct"]] = None
    enable_sniffing: Optional[bool]  = None
    enable_mux:      Optional[bool]  = None
    update_interval: Optional[int]   = Field(None, ge=10, le=86400)


class SettingsResponse(SettingsBase):
    """Full settings representation returned in API responses."""
    id: int

    model_config = ConfigDict(from_attributes=True)
