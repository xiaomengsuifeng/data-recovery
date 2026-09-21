#!/usr/bin/env python3
"""Native Qt/TSK acceptance at one scale, with explicit simulated error checks.

Uses a frozen synthetic image and independent original manifest. Run in separate
processes for 1, 1.25, 1.5 and 2 scales; never changes system display settings.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if '--installed-runtime' not in sys.argv:
    sys.path.insert(0, str(ROOT / 'src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tsk-bin', type=Path, required=True)
    parser.add_argument('--scale', type=float, choices=(1, 1.25, 1.5, 2), default=1)
    parser.add_argument('--installed-runtime', action='store_true')
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    os.environ['QT_SCALE_FACTOR'] = str(args.scale)
    from PySide6.QtWidgets import QApplication, QScrollArea
    import recovery_core
    import recovery_desktop
    from recovery_core.common import RecoveryError, new_directory, read_json, write_json
    from recovery_core.verification import verify
    from recovery_desktop.app import RecoveryWindow, STYLE

    output = new_directory(args.output)
    app = QApplication([])
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = RecoveryWindow(args.tsk_bin)
    errors = []
    window.show_error = errors.append
    window.workspace.setText(str(output))
    window.show()
    available = window.screen().availableGeometry()
    window.move(available.left() + 8, available.top() + 8)
    app.processEvents()

    def require(condition, message):
        if not condition:
            raise RecoveryError(message)

    def wait():
        deadline = time.monotonic() + 180
        while window.worker is not None:
            app.processEvents()
            time.sleep(.01)
            if time.monotonic() > deadline:
                raise RecoveryError('Desktop acceptance worker timed out.')
        app.processEvents()
        if errors:
            raise RecoveryError('; '.join(errors))

    def reachable(control):
        # Scroll innermost first, then expose the control in each outer viewport.
        parent = control.parentWidget()
        while parent:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(control, 4, 4)
                app.processEvents()
            parent = parent.parentWidget()
        parent = control.parentWidget()
        while parent:
            if isinstance(parent, QScrollArea):
                require(parent.viewport().rect().contains(control.mapTo(
                    parent.viewport(), control.rect().center())), 'Control is unreachable by scrolling.')
            parent = parent.parentWidget()
        require(control.height() >= control.minimumSizeHint().height(), 'Control text is clipped vertically.')

    try:
        require(app.platformName() == 'windows', 'Native Windows Qt platform was not used.')
        require(window.frameGeometry().width() <= available.width()
                and window.frameGeometry().height() <= available.height(), 'Window exceeds the available desktop.')
        require(abs(window.devicePixelRatioF() - args.scale) < .01, 'Requested Qt scale did not take effect.')
        window.load_image(args.image)
        wait()
        require(window.partition.count() == 1, 'Expected exactly one synthetic NTFS partition.')
        reachable(window.scan_button)
        window.grab().save(str(output / '01-source.png'))
        window.start_scan()
        wait()
        require(window.proxy.rowCount() > 0, 'No synthetic deleted files found.')
        previews = []
        for item in window.model.items:
            suffix = Path(item['original_path'] or item['observed_path']).suffix.lower()
            kind = 'image' if suffix == '.png' else 'text'
            if kind in previews:
                continue
            window.search.setText(item['original_path'] or item['observed_path'])
            app.processEvents()
            window.table.setCurrentIndex(window.proxy.index(0, 1))
            reachable(window.preview_button)
            window.request_preview()
            wait()
            require(not window.preview_image.pixmap().isNull() if kind == 'image'
                    else bool(window.preview_text.toPlainText()), 'Preview failed.')
            window.grab().save(str(output / ('02-preview-' + kind + '.png')))
            previews.append(kind)
            if len(previews) == 2:
                break
        require(set(previews) == {'image', 'text'}, 'Both image and text previews are required.')
        window.search.clear()
        window.select_visible()
        reachable(window.recover_button)
        window.export_to(output, sorted(window.model.checked))
        wait()
        verification = verify(window.last_output, args.manifest)
        write_json(output / 'verification.json', verification)
        require(verification['all_targets_verified'], 'Synthetic originals did not all match.')
        reachable(window.back_results)
        window.grab().save(str(output / '03-saved.png'))
        window.load_report(window.session, read_json(window.session / 'session.json'))
        window.select_visible()
        original_session, original_selection = window.session, set(window.model.checked)
        with patch('recovery_core.windows._shell_elevate', return_value=1223):
            window.restart_admin()
        require(len(errors) == 1 and '已取消管理员授权' in errors[0], 'Injected UAC cancellation was not explained.')
        require(window.isVisible() and window.session == original_session
                and window.model.checked == original_selection, 'UAC cancellation lost desktop state.')
        errors.clear()
        from recovery_core.control import checkpoint
        def wait_for_cancel():
            while True:
                checkpoint()
                time.sleep(.01)
        window.run_task('Controlled cancellation check', wait_for_cancel, lambda result: None)
        window.cancel_task()
        wait()
        require(window.home_button.isEnabled(), 'Cancellation did not restore controls.')
        write_json(output / 'desktop-acceptance.json', {
            'schema_version': 1, 'status': 'passed', 'scope': 'native_image_and_simulated_errors',
            'qt_platform': app.platformName(), 'scale': args.scale, 'device_pixel_ratio': window.devicePixelRatioF(),
            'available_size': [available.width(), available.height()],
            'frame_size': [window.frameGeometry().width(), window.frameGeometry().height()],
            'core_module': recovery_core.__file__, 'desktop_module': recovery_desktop.__file__,
            'session': str(window.session), 'recovered': str(window.last_output),
            'exact_content_matches': verification['exact_content_matches'],
            'correct_paths': verification['correct_paths'], 'total_targets': verification['total_targets'],
            'previews': previews, 'controls_reachable': True, 'session_reopened': True,
            'worker_cancellation': True, 'uac_cancel_fault_injection': True,
            'real_uac_prompt_cancelled': False, 'physical_media_tested': False})
    finally:
        if window.worker is not None:
            window.cancel_task()
            deadline = time.monotonic() + 35
            while window.worker is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
        window.close()
        app.processEvents()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
