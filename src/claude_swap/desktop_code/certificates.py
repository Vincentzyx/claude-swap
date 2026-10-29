"""A private, host-scoped certificate trusted only by the launched process."""
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile

from claude_swap.locking import FileLock


def prepare(root: Path, extra_ca: str | None = None) -> tuple[ssl.SSLContext, Path]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    cert, key = root / 'api-cert.pem', root / 'api-key.pem'
    ca_cert, ca_key = root / 'ca-cert.pem', root / 'ca-key.pem'
    openssl = shutil.which('openssl')
    if not openssl:
        raise RuntimeError('openssl is required for Desktop Code routing')
    with FileLock(root / 'certificate.lock'):
        valid = all(p.exists() for p in (cert, key, ca_cert, ca_key)) and all(
            subprocess.run([openssl, 'x509', '-checkend', '86400', '-noout', '-in', str(p)],
                           capture_output=True).returncode == 0 for p in (cert, ca_cert))
        if not valid:
            with tempfile.TemporaryDirectory(dir=root) as tmp:
                stage = Path(tmp)
                (stage / 'ca.conf').write_text(
                    '[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
                    '[dn]\nCN=cswap Desktop Code Local CA\n[ext]\n'
                    'basicConstraints=critical,CA:TRUE,pathlen:0\n'
                    'keyUsage=critical,keyCertSign,cRLSign\n'
                    'nameConstraints=critical,permitted;DNS:api.anthropic.com\n')
                (stage / 'leaf.conf').write_text(
                    '[ext]\nbasicConstraints=critical,CA:FALSE\n'
                    'keyUsage=critical,digitalSignature,keyEncipherment\n'
                    'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:api.anthropic.com\n')
                commands = [
                    ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '365',
                     '-keyout', str(stage / 'ca-key'), '-out', str(stage / 'ca-cert'), '-config', str(stage / 'ca.conf')],
                    ['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=api.anthropic.com',
                     '-keyout', str(stage / 'key'), '-out', str(stage / 'csr')],
                    ['x509', '-req', '-in', str(stage / 'csr'), '-CA', str(stage / 'ca-cert'),
                     '-CAkey', str(stage / 'ca-key'), '-CAcreateserial', '-days', '365',
                     '-out', str(stage / 'cert'), '-extfile', str(stage / 'leaf.conf'), '-extensions', 'ext'],
                ]
                for command in commands:
                    subprocess.run([openssl, *command], check=True, capture_output=True)
                for name, dest in [('key', key), ('cert', cert), ('ca-key', ca_key), ('ca-cert', ca_cert)]:
                    os.chmod(stage / name, 0o600)
                    os.replace(stage / name, dest)
        # truststore's global injection is for clients and verifies peers even
        # on a server socket. Construct the server with stdlib SSL, then restore
        # the CLI's native trust for outbound clients. This runs before serving
        # or starting worker threads and uses only truststore's public API.
        import truststore
        injected = ssl.SSLContext is truststore.SSLContext
        if injected:
            truststore.extract_from_ssl()
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
        finally:
            if injected:
                truststore.inject_into_ssl()
        # Keep any trust the caller already configured. No system trust changes.
        bundle = root / 'process-ca.pem'
        data = (Path(extra_ca).read_bytes() + b'\n') if extra_ca else b''
        if extra_ca and Path(extra_ca).resolve() == bundle.resolve():
            raise RuntimeError('Recursive Desktop Code certificate configuration')
        from claude_swap.fsutil import atomic_write_text
        atomic_write_text(bundle, (data + ca_cert.read_bytes()).decode())
        return context, bundle
