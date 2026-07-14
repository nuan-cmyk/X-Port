"""
models.py – SQLAlchemy ORM models.

Defines the three core tables:
  - Node      → proxy server entries
  - Port      → local listener ports mapped to nodes
  - Settings  → singleton application settings row
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Float,
    Text,
)
from sqlalchemy.orm import relationship

from database import Base


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class Node(Base):
    """
    Represents a single remote Xray proxy endpoint (e.g. a VLESS node).

    Columns
    -------
    id         : Auto-increment primary key.
    name       : Human-readable label (e.g. "US-West-01").
    protocol   : Transport protocol identifier (e.g. "vless", "vmess").
    address    : Hostname or IP of the remote server.
    port       : Remote server port.
    uuid       : Proxy UUID / user-id credential.
    network    : Xray network type (e.g. "ws", "tcp", "grpc").
    security   : TLS mode (e.g. "tls", "reality", "none").
    raw_link   : The original share link string (e.g. vless://…).
    latency    : Last measured round-trip latency in milliseconds (nullable).
    """

    __tablename__ = "nodes"

    id       = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name     = Column(String(255), nullable=False)
    protocol = Column(String(50), nullable=False, default="vless")
    address  = Column(String(255), nullable=False)
    port     = Column(Integer, nullable=False)
    uuid     = Column(String(36), nullable=False)
    network  = Column(String(50), nullable=False, default="tcp")
    security = Column(String(50), nullable=False, default="none")
    raw_link = Column(Text, nullable=True)
    latency  = Column(Float, nullable=True)

    # One node can serve multiple local listener ports
    ports = relationship("Port", back_populates="node", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------

class Port(Base):
    """
    Represents a local listener port that tunnels traffic through a Node.

    Columns
    -------
    id           : Auto-increment primary key.
    local_port   : Port number on 127.0.0.1 the proxy listens on.
    protocol     : Local proxy protocol ("http" or "socks").
    node_id      : FK → nodes.id (the upstream exit node).
    status       : Current runtime state ("running" | "stopped").
    auto_assign  : If True the port was automatically assigned by the manager.
    """

    __tablename__ = "ports"

    id          = Column(Integer, primary_key=True, index=True, autoincrement=True)
    local_port  = Column(Integer, nullable=False, unique=True)
    protocol    = Column(String(10), nullable=False, default="socks")
    node_id     = Column(Integer, ForeignKey("nodes.id"), nullable=False)
    status      = Column(String(10), nullable=False, default="stopped")
    auto_assign = Column(Boolean, nullable=False, default=False)

    node = relationship("Node", back_populates="ports")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings(Base):
    """
    Singleton application settings row (always id=1).

    Columns
    -------
    id              : Always 1 (singleton pattern).
    xray_path       : Filesystem path to the xray binary.
    routing_mode    : Active routing strategy ("global" | "rule" | "direct").
    enable_sniffing : Whether Xray domain-sniffing is enabled.
    enable_mux      : Whether Xray connection multiplexing is enabled.
    update_interval : How often (in seconds) the manager re-checks nodes.
    """

    __tablename__ = "settings"

    id              = Column(Integer, primary_key=True, default=1)
    xray_path       = Column(String(512), nullable=False, default="./xray")
    routing_mode    = Column(String(10), nullable=False, default="global")
    enable_sniffing = Column(Boolean, nullable=False, default=True)
    enable_mux      = Column(Boolean, nullable=False, default=False)
    update_interval = Column(Integer, nullable=False, default=300)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class Subscription(Base):
    """
    Represents a remote subscription source (a URL serving a list of nodes).

    Columns
    -------
    id           : Auto-increment primary key.
    name         : Human-readable label for this subscription source.
    url          : Remote URL that returns a Base64-encoded or plain-text
                   list of proxy share links (vless://, vmess://, …).
    last_fetched : UTC timestamp of the last successful fetch (nullable until
                   first successful refresh).
    node_count   : Number of nodes successfully parsed in the last fetch.
    auto_update  : When True the background scheduler will include this
                   subscription in its periodic refresh cycle.
    """

    __tablename__ = "subscriptions"

    id           = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name         = Column(String(255), nullable=False)
    url          = Column(String(1024), nullable=False, unique=True)
    last_fetched = Column(DateTime, nullable=True)
    node_count   = Column(Integer, nullable=False, default=0)
    auto_update  = Column(Boolean, nullable=False, default=True)
