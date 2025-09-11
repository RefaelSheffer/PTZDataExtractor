import sys, pathlib

sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent))

import any_ptz_client


def test_any_ptz_client_fallback(monkeypatch):
    calls = {}

    class DummyOnvif:
        def __init__(self, *a, **kw):
            calls['onvif_init'] = True
        def start(self):
            raise RuntimeError('onvif fail')
        def stop(self):
            calls['onvif_stop'] = True
        def last(self):
            return 1

    class DummyCgi:
        def __init__(self, *a, **kw):
            calls['cgi_init'] = True
        def start(self):
            calls['cgi_start'] = True
        def stop(self):
            calls['cgi_stop'] = True
        def last(self):
            return 2
        poll_dt = 0.2

    monkeypatch.setattr(any_ptz_client, 'OnvifPTZClient', DummyOnvif)
    monkeypatch.setattr(any_ptz_client, 'PtzCgiThread', DummyCgi)
    c = any_ptz_client.AnyPTZClient('h', 80, 'u', 'p')
    c.start()
    assert c.mode == 'cgi'
    assert c.last() == 2
    assert c.poll_dt == DummyCgi.poll_dt
    c.stop()
    assert calls.get('cgi_stop')


def test_any_ptz_client_onvif(monkeypatch):
    class DummyOnvif:
        poll_dt = 0.5
        def __init__(self, *a, **kw):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def last(self):
            return 3

    class DummyCgi:
        def __init__(self, *a, **kw):
            raise AssertionError('should not be used')

    monkeypatch.setattr(any_ptz_client, 'OnvifPTZClient', DummyOnvif)
    monkeypatch.setattr(any_ptz_client, 'PtzCgiThread', DummyCgi)
    c = any_ptz_client.AnyPTZClient('h', 80, 'u', 'p')
    c.start()
    assert c.mode == 'onvif'
    assert c.last() == 3
    assert c.poll_dt == DummyOnvif.poll_dt
    c.stop()
