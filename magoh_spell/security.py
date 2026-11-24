"""Small defensive boundaries; server-side authorization remains essential.

No home-grown encryption or embedded shared secret. Credentials belong to the
QGIS authentication database, never to an exported project or the plugin ZIP.
"""
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import re
import stat


class SecurityError(ValueError):
    pass


def https_origin(url):
    try:
        parts = urlsplit(str(url))
        if (parts.scheme.lower() != 'https' or not parts.hostname or
                parts.username is not None or parts.password is not None or parts.fragment or
                any(c in str(url) for c in ('\r', '\n', '\x00'))):
            raise ValueError()
        return parts.hostname.lower(), parts.port or 443
    except ValueError as exc:
        raise SecurityError('Indirizzo HTTPS non valido / Invalid HTTPS address') from exc


def validate_api_target(base_url, target):
    if https_origin(base_url) != https_origin(target):
        raise SecurityError('Destinazione API diversa da quella configurata / API destination differs from configured server')


def redact(message, secrets=()):
    text = str(message or '')
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s,;"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'''(?i)((?:token|password|authorization|api[_-]?key)["']?\s*[=:]\s*["']?)[^&\s,;"'}]+''', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/@\s]+@', r'\1[REDACTED]@', text)
    return text[:3000]


def store_bearer(token, authcfg='', base_url=''):
    """Only remove a legacy plaintext token after encrypted storage succeeds."""
    token = str(token)
    if not token or len(token) > 16384 or any(c in token for c in ('\r','\n','\x00')):
        raise SecurityError('Token non valido / Invalid token')
    from qgis.core import QgsApplication, QgsAuthMethodConfig
    manager = QgsApplication.authManager()
    if manager.isDisabled():
        raise SecurityError('Archivio credenziali QGIS non disponibile / QGIS credential store unavailable')
    if not manager.setMasterPassword(True):
        raise SecurityError('Credenziali non salvate: archivio QGIS bloccato / Credentials not saved: QGIS store locked')
    config = QgsAuthMethodConfig()
    if authcfg:
        ok, config = manager.loadAuthenticationConfig(authcfg, config, True)
        if not ok:
            raise SecurityError('Configurazione credenziali non leggibile / Credential configuration cannot be read')
        if config.name() != 'MAGOH Spell API' or https_origin(config.uri()) != https_origin(base_url):
            raise SecurityError('Configurazione credenziali diversa: nessun valore sovrascritto / Different credential configuration: nothing overwritten')
    config.setName('MAGOH Spell API')
    config.setUri(base_url)
    config.setMethod('APIHeader')
    config.setConfig('Authorization', 'Bearer ' + str(token))
    ok, config = manager.storeAuthenticationConfig(config, bool(authcfg))
    if not ok or not config.id():
        raise SecurityError('Salvataggio protetto credenziali non riuscito / Secure credential storage failed')
    return config.id()


def authenticate_request(request, authcfg):
    from qgis.core import QgsApplication, QgsAuthMethodConfig
    manager = QgsApplication.authManager()
    ok, config = manager.loadAuthenticationConfig(authcfg, QgsAuthMethodConfig(), False)
    if not ok or https_origin(config.uri()) != https_origin(request.url().toString()):
        raise SecurityError('Credenziali associate a un altro server / Credentials belong to a different server')
    ok, request = manager.updateNetworkRequest(request, authcfg)
    if not ok:
        raise SecurityError('Autenticazione QGIS non disponibile / QGIS authentication unavailable')
    return request


def safe_extract(archive, destination, max_bytes=256 * 1024 * 1024, max_files=2048):
    """Validate the complete archive before writing, never overwrite files.

    Path traversal, symlinks, Windows aliases and duplicate destinations are
    rejected. A failed archive cannot write outside its fresh output folder.
    """
    destination = Path(destination).resolve()
    infos = archive.infolist()
    if len(infos) > max_files or sum(i.file_size for i in infos) > max_bytes:
        raise SecurityError('Archivio troppo grande / Archive exceeds safe limits')
    planned, seen = [], set()
    for info in infos:
        name = info.filename.replace('\\', '/')
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith('/') or any(p in ('.', '..') or ':' in p or
                p.endswith((' ', '.')) or any(ord(c) < 32 for c in p) or
                any(c in p for c in '<>"|?*') or
                p.split('.')[0].rstrip(' ').upper() in {'CON','PRN','AUX','NUL', *('COM%d' % n for n in range(1,10)), *('LPT%d' % n for n in range(1,10))}
                for p in parts) or stat.S_ISLNK(info.external_attr >> 16)):
            raise SecurityError('Percorso non sicuro nello ZIP / Unsafe ZIP path')
        target = destination.joinpath(*parts).resolve()
        if not target.is_relative_to(destination):
            raise SecurityError('Percorso esterno alla destinazione / Path escapes destination')
        key = str(target).casefold()
        if key in seen or target.exists():
            raise SecurityError('File duplicato o già esistente / Duplicate or existing archive target')
        seen.add(key); planned.append((info, target))
    for info, target in planned:
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open('xb') as dst:
                written = 0
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size or written > max_bytes:
                        raise SecurityError('Dimensioni ZIP incoerenti / Inconsistent archive size')
                    dst.write(chunk)
