"""Only explicit local demo; no automatic cloud configuration or credentials."""
import argparse
from datetime import UTC,datetime,timedelta
from pathlib import Path
import socket
import sys

from . import require_demo,NOTICE


def isolate_network():
    """Process-wide defense for this demo executable only; no SDK credential IO."""
    connect,connect_ex,dns=socket.socket.connect,socket.socket.connect_ex,socket.getaddrinfo
    def allowed(address):
        return isinstance(address,tuple) and address[0] in ('127.0.0.1','::1')
    def guarded(self,address):
        if not allowed(address): raise RuntimeError('demo_external_network_disabled')
        return connect(self,address)
    def guarded_ex(self,address):
        if not allowed(address): raise RuntimeError('demo_external_network_disabled')
        return connect_ex(self,address)
    def guarded_dns(host,*args,**kwargs):
        if host not in ('127.0.0.1','::1',b'127.0.0.1',b'::1'):
            raise RuntimeError('demo_external_dns_disabled')
        return dns(host,*args,**kwargs)
    socket.socket.connect,socket.socket.connect_ex,socket.getaddrinfo=guarded,guarded_ex,guarded_dns
    # Importing credential base types is required by the existing reader module;
    # discovery/ADC/file loaders are forbidden before product startup.
    import google.auth
    import google.auth._default
    def no_credentials(*args,**kwargs):
        raise RuntimeError('demo_credentials_disabled')
    for module in (google.auth,google.auth._default):
        for name in ('default','load_credentials_from_file','load_credentials_from_dict'):
            if hasattr(module,name): setattr(module,name,no_credentials)
    class NoCloud:
        def find_spec(self,fullname,path=None,target=None):
            if fullname.startswith(('google.cloud','google.genai','vertexai','googleapiclient','grpc')):
                raise RuntimeError('demo_cloud_sdk_disabled')
    sys.meta_path.insert(0,NoCloud())


def certificate(directory):
    from cryptography import x509
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes,serialization
    import ipaddress
    keypath,certpath=directory/'localhost.key',directory/'localhost.pem'
    if certpath.exists() and keypath.exists():
        cert=x509.load_pem_x509_certificate(certpath.read_bytes())
        if cert.not_valid_after_utc>datetime.now(UTC)+timedelta(hours=1):
            return certpath,keypath
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'CampusPulse LOCAL DEMO')])
    now=datetime.now(UTC)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
        .not_valid_after(now+timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
        .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
        .sign(key,hashes.SHA256()))
    keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return certpath,keypath


def main():
    parser=argparse.ArgumentParser(description='CampusPulse loopback-only test demo; no Google/Gemini calls')
    parser.add_argument('--demo',action='store_true',help='Explicitly select test adapters')
    parser.add_argument('--port',type=int,default=8947)
    args=parser.parse_args()
    require_demo(args.demo)
    if not 1024<=args.port<=65535: parser.error('port must be 1024..65535')
    isolate_network()
    root=Path.cwd()
    if not (root/'pyproject.toml').is_file(): parser.error('run from the recovery_app directory')
    runtime=root/'.validation/mvp-20261019/step01/runtime'
    runtime.mkdir(parents=True,exist_ok=True)
    cert,key=certificate(runtime)
    import uvicorn
    from .app import create_demo_app
    origin='https://127.0.0.1:'+str(args.port)
    app,_=create_demo_app(demo=True,origin=origin)
    print(NOTICE)
    print('Open '+origin+'/demo (manual local certificate warning; never enter Google credentials).',flush=True)
    import asyncio
    config=uvicorn.Config(app,host='127.0.0.1',port=args.port,ssl_certfile=str(cert),ssl_keyfile=str(key),
                         proxy_headers=False,access_log=False,log_level='warning')
    # A selector loop also avoids Windows Proactor TLS disconnect callback noise.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(uvicorn.Server(config).serve())


if __name__=='__main__': main()
