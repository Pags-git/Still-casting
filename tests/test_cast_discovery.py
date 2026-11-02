import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from casting import CastManager
from dlna import DlnaDevice


class DiscoveryTests(unittest.TestCase):
    def test_shared_list_prefers_google_and_retains_active_connection(self):
        google=SimpleNamespace(uuid='sony',name='Sony',cast_info=SimpleNamespace(host='192.168.1.2'),disconnect=Mock())
        duplicate=SimpleNamespace(uuid='sony',name='Sony',cast_info=google.cast_info,disconnect=Mock())
        sony_dlna=DlnaDevice('dlna:sony','Sony','192.168.1.2','http://192.168.1.2/control','urn:schemas-upnp-org:service:AVTransport:1')
        samsung=DlnaDevice('dlna:samsung','Samsung','192.168.1.3','http://192.168.1.3/control','urn:schemas-upnp-org:service:AVTransport:1')
        manager=CastManager(); manager.devices=[google]; manager.cast=google
        with patch('pychromecast.get_chromecasts',return_value=([duplicate],Mock())), patch('casting.discover_dlna',return_value=[sony_dlna,samsung]):
            manager.discover(); manager.executor.submit(lambda:None).result(5)
        self.assertEqual(manager.devices,[google,samsung])
        self.assertIs(manager.cast,google)
        google.disconnect.assert_not_called()
        duplicate.disconnect.assert_called_once()
        manager.cast=None
        with patch('pychromecast.discovery.stop_discovery'):
            manager.close(); manager.executor.shutdown(wait=True)

    def test_dlna_uses_shared_start_and_pause_stop(self):
        device=DlnaDevice('dlna:samsung','Samsung','192.168.1.3','http://192.168.1.3/control','urn:schemas-upnp-org:service:AVTransport:1')
        manager=CastManager(); manager.devices=[device]
        server=Mock(url='http://local/video',mime='video/mp4')
        with patch('casting.socket.socket') as socket, patch('casting.MediaServer',return_value=server), patch.object(DlnaDevice,'load',return_value='') as load, patch.object(DlnaDevice,'pause') as pause, patch.object(DlnaDevice,'stop') as stop:
            socket.return_value.__enter__.return_value.getsockname.return_value=('192.168.1.4',1234)
            manager.start(device.uuid,'/tmp/movie.mp4',20000); manager.executor.submit(lambda:None).result(5)
            self.assertIs(manager.cast,device)
            load.assert_called_once()
            manager.command('pause'); manager.executor.submit(lambda:None).result(5); pause.assert_called_once()
            manager.command('stop'); manager.executor.submit(lambda:None).result(5); stop.assert_called_once()
            server.close.assert_called_once(); self.assertIsNone(manager.cast)
        manager.close(); manager.executor.shutdown(wait=True)
