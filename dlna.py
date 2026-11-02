"""Small UPnP AVTransport adapter for the shared casting controls."""
from dataclasses import dataclass
import socket
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

OPENER = build_opener(ProxyHandler({}))
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'


def read_xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsupported XML declaration')
    return ET.fromstring(data)


def fetch(request):
    with OPENER.open(request, timeout=3) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError('TV response is too large')
    return data


@dataclass
class DlnaDevice:
    uuid: str
    name: str
    host: str
    control_url: str
    service: str

    @property
    def cast_info(self):
        return SimpleNamespace(host=self.host)

    @property
    def media_controller(self):
        return self

    def wait(self, timeout=10):
        pass

    def disconnect(self, timeout=2):
        pass

    def action(self, name, **args):
        root = ET.Element(ET.QName(SOAP, 'Envelope'), {ET.QName(SOAP, 'encodingStyle'): 'http://schemas.xmlsoap.org/soap/encoding/'})
        body = ET.SubElement(root, ET.QName(SOAP, 'Body'))
        action = ET.SubElement(body, ET.QName(self.service, name))
        for key, value in {'InstanceID': 0, **args}.items():
            ET.SubElement(action, key).text = str(value)
        request = Request(self.control_url, data=ET.tostring(root, encoding='utf-8', xml_declaration=True),
                          headers={'Content-Type': 'text/xml; charset="utf-8"', 'SOAPAction': f'"{self.service}#{name}"'})
        try:
            result = read_xml(fetch(request))
        except HTTPError as error:
            try:
                fault = read_xml(error.read(65536))
                detail = fault.findtext('.//{*}errorDescription') or str(error.code)
            except (ValueError, ET.ParseError):
                detail = str(error.code)
            raise RuntimeError(f'{self.name}: {name} rejected ({detail}). Check the TV allows media sharing.') from error
        fault = result.find('.//{*}Fault')
        if fault is not None:
            raise RuntimeError(f'{self.name}: {name} failed')
        return result

    def load(self, url, mime, title, position, cancelled):
        metadata = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                    '<item id="0" parentID="-1" restricted="1">'
                    f'<dc:title>{escape(title)}</dc:title><upnp:class>object.item.videoItem</upnp:class>'
                    f'<res protocolInfo="http-get:*:{escape(mime)}:DLNA.ORG_OP=01;DLNA.ORG_CI=0">{escape(url)}</res>'
                    '</item></DIDL-Lite>')
        self.action('SetAVTransportURI', CurrentURI=url, CurrentURIMetaData=metadata)
        self.play()
        deadline = time.monotonic() + 15
        while not cancelled.is_set() and time.monotonic() < deadline:
            state = self.action('GetTransportInfo').findtext('.//{*}CurrentTransportState')
            if state in ('PLAYING', 'PAUSED_PLAYBACK'):
                if position > 0:
                    seconds = position // 1000
                    try:
                        self.action('Seek', Unit='REL_TIME', Target=f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}')
                    except RuntimeError:
                        return 'The TV does not support seeking; playback started from the beginning.'
                return ''
            cancelled.wait(.3)
        raise RuntimeError(f'{self.name} did not start playback. Check the format and any sharing permission shown on the TV.')

    def play(self):
        self.action('Play', Speed=1)

    def pause(self):
        self.action('Pause')

    def stop(self):
        self.action('Stop')


def parse_device(data, location):
    root = read_xml(data)
    base = root.findtext('{*}URLBase') or location
    result = []
    for device in root.findall('.//{*}device'):
        for service in device.findall('./{*}serviceList/{*}service'):
            kind = service.findtext('{*}serviceType') or ''
            if not kind.startswith('urn:schemas-upnp-org:service:AVTransport:'):
                continue
            control = urljoin(base, service.findtext('{*}controlURL') or '')
            if urlsplit(control).scheme != 'http' or urlsplit(control).hostname != urlsplit(location).hostname:
                continue
            uid = device.findtext('{*}UDN') or location
            result.append(DlnaDevice('dlna:' + uid, device.findtext('{*}friendlyName') or 'TV',
                                     urlsplit(control).hostname, control, kind))
    return result


def discover_dlna(timeout=4):
    locations = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(.3)
        for target in ('urn:schemas-upnp-org:device:MediaRenderer:1', 'urn:schemas-upnp-org:service:AVTransport:1'):
            request = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                       f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {target}\r\n\r\n')
            sock.sendto(request.encode(), ('239.255.255.250', 1900))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet, sender = sock.recvfrom(65535)
            except socket.timeout:
                continue
            headers = {}
            for line in packet.decode('utf-8', errors='replace').splitlines()[1:]:
                key, sep, value = line.partition(':')
                if sep:
                    headers[key.strip().lower()] = value.strip()
            location = headers.get('location', '')
            parsed = urlsplit(location)
            # Device descriptions should be hosted by the responding LAN device.
            if parsed.scheme == 'http' and parsed.hostname == sender[0] and not parsed.username:
                locations.add(location)
    devices = {}
    for location in sorted(locations)[:32]:
        try:
            for device in parse_device(fetch(location), location):
                devices[device.uuid] = device
        except (OSError, ValueError, ET.ParseError):
            continue
    return list(devices.values())
