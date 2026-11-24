"""rovimen_mode_switch must drive SwitchMode on both RMS cameraControl signatures.

RMS#1001 (September 2026) inserted camera_user/camera_pwd before cmd; the old
positional call then raised TypeError every cron run and the relay stations'
cameras stayed in whichever mode they were last switched to.
"""
import rovimen_mode_switch as ms


def test_master_signature_gets_the_old_call():
    calls = []

    def camera_control(camera_ip, cmd, opts='', camera_settings_path='./camera_settings.json'):
        calls.append((camera_ip, cmd, opts, camera_settings_path))

    ms.switch_mode(camera_control, '192.168.42.10', 'night', '/s.json')
    assert calls == [('192.168.42.10', 'SwitchMode', ['night'], '/s.json')]


def test_release_signature_gets_the_default_credentials():
    calls = []

    def camera_control(camera_ip, camera_user, camera_pwd, cmd, opts='',
                       camera_settings_path='./camera_settings.json'):
        calls.append((camera_ip, camera_user, camera_pwd, cmd, opts, camera_settings_path))

    ms.switch_mode(camera_control, '192.168.42.10', 'day', '/s.json')
    assert calls == [('192.168.42.10', 'admin', '', 'SwitchMode', ['day'], '/s.json')]
