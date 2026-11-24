"""Client REST MAGOH per QGIS.

Il connettore e' intenzionalmente *read only*: usa esclusivamente richieste HTTP
GET e serve a scaricare snapshot JSON/GeoJSON nel progetto QGIS. Non contiene
funzioni di scrittura verso MAGOH e non dipende dagli endpoint di export GNA.

Gli endpoint predefiniti seguono il manuale SearchController MAGOH (modulo v0).
Il dominio di produzione comunicato per il collaudo e' preconfigurato, mentre
resta modificabile il solo Base URL per facilitare eventuali ambienti futuri.
Gli endpoint ICA/GNA non sono usati da questo client.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from qgis.PyQt.QtCore import QObject, QSettings, QTimer, QUrl, QUrlQuery, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsNetworkAccessManager
from .security import validate_api_target, redact, store_bearer, authenticate_request, SecurityError


API_PREFIX = "magoh_spell/api/"
DEFAULT_API_BASE_URL = "https://magoh-api.cfs.unipi.it"


@dataclass
class MagohApiSettings:
    base_url: str = DEFAULT_API_BASE_URL
    search_endpoint: str = "/v0/search/search-adv"
    health_endpoint: str = "/v0/search/stats"
    facet_endpoint: str = "/v0/search/get-facet"
    auth_mode: str = "none"  # none | bearer
    token: str = field(default="", repr=False)
    authcfg: str = ""
    page_size: int = 200

    @classmethod
    def load(cls) -> "MagohApiSettings":
        s = QSettings()
        try:
            page_size = int(s.value(API_PREFIX + "page_size", 200) or 200)
        except Exception:
            page_size = 200
        page_size = max(20, min(5000, page_size))
        return cls(
            base_url=str(s.value(API_PREFIX + "base_url", DEFAULT_API_BASE_URL) or DEFAULT_API_BASE_URL),
            search_endpoint=str(
                s.value(API_PREFIX + "search_endpoint", "/v0/search/search-adv")
                or "/v0/search/search-adv"
            ),
            health_endpoint=str(
                s.value(API_PREFIX + "health_endpoint", "/v0/search/stats")
                or "/v0/search/stats"
            ),
            facet_endpoint=str(
                s.value(API_PREFIX + "facet_endpoint", "/v0/search/get-facet")
                or "/v0/search/get-facet"
            ),
            auth_mode=str(s.value(API_PREFIX + "auth_mode", "none") or "none"),
            token=str(s.value(API_PREFIX + "token", "") or ""),
            authcfg=str(s.value(API_PREFIX + "authcfg", "") or ""),
            page_size=page_size,
        )

    def save(self) -> None:
        s = QSettings()
        s.setValue(API_PREFIX + "base_url", self.base_url.strip())
        s.setValue(API_PREFIX + "search_endpoint", self.search_endpoint.strip())
        s.setValue(API_PREFIX + "health_endpoint", self.health_endpoint.strip())
        s.setValue(API_PREFIX + "facet_endpoint", self.facet_endpoint.strip())
        s.setValue(API_PREFIX + "auth_mode", self.auth_mode.strip() or "none")
        if self.token:
            self.authcfg = store_bearer(self.token, self.authcfg, self.base_url)
            s.setValue(API_PREFIX + "authcfg", self.authcfg)
            s.sync()
            if s.status() != QSettings.Status.NoError:
                raise SecurityError('Impossibile salvare il riferimento protetto: token precedente conservato / Cannot save secure reference: previous token retained')
            s.remove(API_PREFIX + "token")
            # V1.15 copied legacy settings once. Remove only this migrated secret.
            s.remove("magoh_offline/api/token")
            s.sync()
            self.token = ""
        elif self.authcfg:
            s.setValue(API_PREFIX + "authcfg", self.authcfg)
        s.setValue(API_PREFIX + "page_size", int(self.page_size))

    def resolve(self, endpoint: str) -> str:
        endpoint = (endpoint or "").strip()
        base = (self.base_url or "").strip().rstrip("/")
        if not endpoint:
            return base
        if endpoint.lower().startswith(("http://", "https://")):
            return endpoint
        if not base:
            return endpoint
        return base + "/" + endpoint.lstrip("/")

    def endpoint_for(self, kind: str) -> str:
        mapping = {
            "health": self.health_endpoint,
            "search": self.search_endpoint,
            "facet": self.facet_endpoint,
        }
        return self.resolve(mapping.get(kind, ""))

    def build_url(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
        target = self.resolve(endpoint)
        if not target:
            return ""
        url = QUrl(target)
        query = QUrlQuery(url)
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            query.addQueryItem(str(key), str(value))
        url.setQuery(query)
        return url.toString()


    def search_url(
        self,
        filters: Optional[Dict[str, Any]] = None,
        start: int = 0,
        rows: Optional[int] = None,
    ) -> str:
        params = dict(filters or {})
        params["start"] = max(0, int(start))
        params["rows"] = max(1, int(rows or self.page_size))
        return self.build_url(self.search_endpoint, params)


    def facet_url(self, facet: str, query: str = "") -> str:
        params: Dict[str, Any] = {"facet": facet}
        if query:
            params["query"] = query
        return self.build_url(self.facet_endpoint, params)


class MagohApiClient(QObject):
    """Client JSON asincrono basato sul networking nativo di QGIS."""

    succeeded = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    def __init__(self, settings: Optional[MagohApiSettings] = None, parent=None):
        super().__init__(parent)
        self.settings = settings or MagohApiSettings.load()
        self._replies = set()
        self._timers = {}
        self._timed_out = set()
        self._buffers = {}
        self._oversized = set()

    MAX_RESPONSE_BYTES = 64 * 1024 * 1024

    def _consume(self, reply):
        if reply not in self._replies:
            return
        data = bytes(reply.readAll())
        buffer = self._buffers.setdefault(reply, bytearray())
        if len(buffer) + len(data) > self.MAX_RESPONSE_BYTES:
            self._oversized.add(reply)
            buffer.clear()
            reply.abort()
        else:
            buffer.extend(data)

    def cancel(self):
        """Abort outstanding requests without emitting stale results/errors."""
        replies = tuple(self._replies)
        self._replies.clear()
        for reply in replies:
            timer = self._timers.pop(reply, None)
            if timer is not None:
                timer.stop()
            reply.abort()
            self._buffers.pop(reply, None)

    def _timeout(self, reply):
        if reply in self._replies:
            self._timed_out.add(reply)
            reply.abort()

    def get_json(self, kind: str, url: Optional[str] = None) -> None:
        target = (url or self.settings.endpoint_for(kind) or "").strip()
        if not target:
            self.failed.emit(kind, "Endpoint non configurato / endpoint not configured")
            return

        try:
            validate_api_target(self.settings.base_url, target)
            if self.settings.auth_mode not in ('none', 'bearer'):
                raise SecurityError('Metodo di autenticazione non valido / Invalid authentication method')
            request = QNetworkRequest(QUrl(target))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy)
            if self.settings.auth_mode == "bearer":
                if self.settings.token:
                    self.settings.save()  # migrate only when the connection needs it
                if not self.settings.authcfg:
                    raise SecurityError('Token API non configurato / API token not configured')
                request = authenticate_request(request, self.settings.authcfg)
        except Exception as exc:
            self.failed.emit(kind, redact(exc, (self.settings.token,)))
            return
        request.setRawHeader(b"Accept", b"application/json, application/geo+json;q=0.9, */*;q=0.1")
        request.setRawHeader(b"User-Agent", b"MAGOH-Spell-QGIS")
        reply = QgsNetworkAccessManager.instance().get(request)
        reply.setReadBufferSize(1024 * 1024)
        self._replies.add(reply)
        self._buffers[reply] = bytearray()
        reply.readyRead.connect(lambda r=reply: self._consume(r))
        reply.finished.connect(lambda r=reply, k=kind: self._finished(k, r))
        timer = QTimer(reply)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda r=reply: self._timeout(r))
        self._timers[reply] = timer
        timer.start(60000)

    def _finished(self, kind: str, reply) -> None:
        try:
            timer = self._timers.pop(reply, None)
            if timer is not None:
                timer.stop()
            if reply not in self._replies:
                self._timed_out.discard(reply)
                return
            self._replies.remove(reply)
            if reply in self._oversized:
                self.failed.emit(kind, 'Risposta API troppo grande: ridurre i record per pagina / API response too large: reduce page size')
                return
            if reply in self._timed_out:
                self._timed_out.remove(reply)
                self.failed.emit(kind, 'Timeout: 60 s. Riprova / Try again.')
                return
            err = reply.error()
            if err != QNetworkReply.NetworkError.NoError:
                self.failed.emit(kind, redact(reply.errorString() or f"Network error {getattr(err, 'value', err)}", (self.settings.token,)))
                return
            buffer = self._buffers.get(reply, bytearray())
            tail = bytes(reply.readAll())
            if len(buffer) + len(tail) > self.MAX_RESPONSE_BYTES:
                self.failed.emit(kind, 'Risposta API troppo grande / API response too large')
                return
            buffer.extend(tail)
            raw = buffer.decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except Exception as exc:
                self.failed.emit(kind, f"Risposta non JSON / non-JSON response: {exc}")
                return
            self.succeeded.emit(kind, payload)
        finally:
            self._buffers.pop(reply, None)
            self._oversized.discard(reply)
            self._timed_out.discard(reply)
            try:
                reply.deleteLater()
            except Exception:
                pass

    @staticmethod
    def _json_value(value: Any) -> Any:
        """Decodifica ricorsivamente valori JSON eventualmente serializzati come stringa.

        Alcuni endpoint Yii/PostGIS possono restituire un GeoJSON come stringa dentro
        un wrapper (ad es. ``data`` o ``geojson``) invece che come oggetto JSON gia'
        deserializzato. Il client deve accettare entrambe le forme.
        """
        current = value
        for _ in range(3):
            if not isinstance(current, str):
                break
            text = current.strip()
            if not text or text[0] not in '{[':
                break
            try:
                current = json.loads(text)
            except Exception:
                # Alcuni output GeoJSON di SearchController contengono virgole finali
                # prima di } o ]. Sono tollerate dai generatori lato server ma non
                # dallo standard JSON/Python. Normalizziamo solo questo difetto noto.
                repaired = re.sub(r",\s*([}\]])", r"\1", text)
                try:
                    current = json.loads(repaired)
                except Exception:
                    break
        return current


    @staticmethod
    def extract_search_docs(payload: Any) -> List[Dict[str, Any]]:
        """Estrae i documenti completi restituiti da search-adv.

        Accetta sia il payload Solr classico ``response.docs`` sia la forma
        alleggerita ``{numFound, docs}`` osservata negli snapshot reali.
        """
        obj = MagohApiClient._json_value(payload)
        for _ in range(5):
            if not isinstance(obj, dict):
                return []
            if isinstance(obj.get("docs"), list):
                return [x for x in obj.get("docs", []) if isinstance(x, dict)]
            response = MagohApiClient._json_value(obj.get("response"))
            if isinstance(response, dict) and isinstance(response.get("docs"), list):
                return [x for x in response.get("docs", []) if isinstance(x, dict)]
            moved = False
            for key in ("data", "result", "payload"):
                child = MagohApiClient._json_value(obj.get(key))
                if isinstance(child, dict):
                    obj = child
                    moved = True
                    break
            if not moved:
                break
        return []

    @staticmethod
    def extract_search_total(payload: Any) -> Optional[int]:
        obj = MagohApiClient._json_value(payload)
        for _ in range(5):
            if not isinstance(obj, dict):
                return None
            try:
                if obj.get("numFound") is not None:
                    return int(obj.get("numFound"))
            except Exception:
                pass
            response = MagohApiClient._json_value(obj.get("response"))
            if isinstance(response, dict):
                try:
                    return int(response.get("numFound"))
                except Exception:
                    pass
            moved = False
            for key in ("data", "result", "payload"):
                child = MagohApiClient._json_value(obj.get(key))
                if isinstance(child, dict):
                    obj = child
                    moved = True
                    break
            if not moved:
                break
        return None


    @staticmethod
    def extract_facet_values(payload: Any, facet_name: str = "") -> List[str]:
        """Estrae valori di faccetta da risposte Yii/Solr in forme eterogenee.

        Supporta `terms` dell'endpoint MAGOH get-facet, array semplici,
        liste di oggetti label/value/name/term e la
        struttura Solr ``facet_counts.facet_fields`` (anche nel formato
        alternato valore/conteggio).
        """
        obj = MagohApiClient._json_value(payload)
        names = []

        def add(value):
            if value is None:
                return
            text = str(value).strip()
            if text and text not in names:
                names.append(text)

        def walk(value, depth=0):
            if depth > 8:
                return
            value = MagohApiClient._json_value(value)
            if isinstance(value, list):
                # Solr puo' restituire [valore, count, valore, count, ...].
                if value and all(not isinstance(x, (dict, list)) for x in value):
                    looks_pairs = len(value) >= 2 and len(value) % 2 == 0 and all(
                        isinstance(value[i], (int, float)) or str(value[i]).isdigit()
                        for i in range(1, len(value), 2)
                    )
                    seq = value[0::2] if looks_pairs else value
                    for item in seq:
                        add(item)
                    return
                for item in value:
                    if isinstance(item, dict):
                        for key in ("label", "value", "name", "term", "key"):
                            if item.get(key) not in (None, ""):
                                add(item.get(key)); break
                        else:
                            walk(item, depth + 1)
                    else:
                        add(item)
                return
            if not isinstance(value, dict):
                return

            # Solr classico.
            fc = value.get("facet_counts")
            if isinstance(fc, dict):
                ff = fc.get("facet_fields")
                if isinstance(ff, dict):
                    preferred = []
                    if facet_name:
                        preferred.extend([facet_name, facet_name + "_s"])
                    else:
                        preferred.extend(ff.keys())
                    seen = set()
                    for key in preferred:
                        if key in seen or key not in ff:
                            continue
                        seen.add(key)
                        walk(ff.get(key), depth + 1)
                        if names:
                            return

            # Production get-facet: {facet: 'provincia', terms: [{name, count}]}.
            # A response for a different facet must not populate this menu.
            declared = value.get('facet')
            if isinstance(declared, str) and facet_name and declared not in (facet_name, facet_name + '_s'):
                return
            for key in ("terms", "values", "items", "results", "data", "result", "payload", "response", "facets", "facet"):
                if key in value:
                    walk(value.get(key), depth + 1)
                    if names:
                        return

            # Alcuni endpoint restituiscono {"Pisa": 123, ...}.
            if value and all(not isinstance(v, (dict, list)) for v in value.values()):
                numeric_values = all(
                    isinstance(v, (int, float)) or str(v).isdigit() for v in value.values()
                )
                if numeric_values:
                    for key in value.keys():
                        add(key)

        walk(obj)
        return sorted(names, key=lambda x: x.casefold())

