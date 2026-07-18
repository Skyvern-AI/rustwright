"""Compatibility for imports of Playwright's private API-structures module.

Most structures are the same ``TypedDict`` classes Rustwright already exposes
publicly; the few without a public Rustwright equivalent are defined here with
upstream Playwright's field shapes.
"""

from os import PathLike
from typing import Literal, Optional, TypedDict, Union

from rustwright.sync_api import (  # noqa: F401
    Cookie,
    FilePayload,
    FloatRect,
    Geolocation,
    HttpCredentials,
    PdfMargins,
    Position,
    ProxySettings,
    ResourceTiming,
    SourceLocation,
    StorageState,
    ViewportSize,
)


class SetCookieParam(TypedDict, total=False):
    name: str
    value: str
    url: Optional[str]
    domain: Optional[str]
    path: Optional[str]
    expires: Optional[float]
    httpOnly: Optional[bool]
    secure: Optional[bool]
    sameSite: Optional[Literal["Lax", "None", "Strict"]]
    partitionKey: Optional[str]


class ClientCertificate(TypedDict, total=False):
    origin: str
    certPath: Optional[Union[str, PathLike]]
    keyPath: Optional[Union[str, PathLike]]
    pfxPath: Optional[Union[str, PathLike]]
    cert: Optional[bytes]
    key: Optional[bytes]
    pfx: Optional[bytes]
    passphrase: Optional[str]


class RemoteAddr(TypedDict):
    ipAddress: str
    port: int


class SecurityDetails(TypedDict, total=False):
    issuer: Optional[str]
    protocol: Optional[str]
    subjectName: Optional[str]
    validFrom: Optional[float]
    validTo: Optional[float]


class RequestSizes(TypedDict):
    requestBodySize: int
    requestHeadersSize: int
    responseBodySize: int
    responseHeadersSize: int


class NameValue(TypedDict):
    name: str
    value: str


__all__ = [
    "ClientCertificate",
    "Cookie",
    "FilePayload",
    "FloatRect",
    "Geolocation",
    "HttpCredentials",
    "NameValue",
    "PdfMargins",
    "Position",
    "ProxySettings",
    "RemoteAddr",
    "RequestSizes",
    "ResourceTiming",
    "SecurityDetails",
    "SetCookieParam",
    "SourceLocation",
    "StorageState",
    "ViewportSize",
]
