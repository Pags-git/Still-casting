import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import xml.etree.ElementTree as ET
from dlna import DlnaDevice, parse_device


class DlnaTests(unittest.TestCase):
    def test_description_and_controls(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = ET.fromstring(self.rfile.read(int(self.headers['Content-Length'])))
                action = list(body.find('{*}Body'))[0]
                name = action.tag.split('}')[-1]
                args = {e.tag: e.text for e in action}
                calls.append((name, args))
                text = '<CurrentTransportState>PLAYING</CurrentTransportState>' if name == 'GetTransportInfo' else ''
                response = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                            f'<u:{name}Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">{text}'
                            f'</u:{name}Response></s:Body></s:Envelope>').encode()
                self.send_response(200); self.send_header('Content-Length',str(len(response)))
                self.end_headers(); self.wfile.write(response)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            location=f'http://127.0.0.1:{server.server_port}/description.xml'
            description=b'''<root xmlns="urn:schemas-upnp-org:device-1-0"><device><friendlyName>Samsung TV</friendlyName><UDN>uuid:samsung</UDN><serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/control</controlURL></service></serviceList></device></root>'''
            devices=parse_device(description,location)
            self.assertEqual(len(devices),1)
            device=devices[0]
            self.assertEqual(device.name,'Samsung TV')
            self.assertEqual(device.control_url,location.replace('/description.xml','/control'))
            self.assertEqual(device.load('http://127.0.0.1/video','video/mp4','Movie & title',62000,threading.Event()),'')
            device.pause(); device.play(); device.stop()
            names=[name for name,args in calls]
            self.assertEqual(names,['SetAVTransportURI','Play','GetTransportInfo','Seek','Pause','Play','Stop'])
            metadata=ET.fromstring(calls[0][1]['CurrentURIMetaData'])
            self.assertEqual(metadata.findtext('.//{*}title'),'Movie & title')
            self.assertEqual(calls[3][1]['Target'],'00:01:02')
            self.assertTrue(all(args['InstanceID']=='0' for name,args in calls))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_external_control_endpoint_rejected(self):
        description=b'<root><device><serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>http://different/control</controlURL></service></serviceList></device></root>'
        self.assertEqual(parse_device(description,'http://127.0.0.1/device.xml'),[])

if __name__=='__main__': unittest.main()
