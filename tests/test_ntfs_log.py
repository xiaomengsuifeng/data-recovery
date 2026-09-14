"""Independent byte fixtures: no Windows acceptance offsets/names are embedded."""
import copy
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_core import ntfs_log
from recovery_core.cli import main
from recovery_core.common import RecoveryError, read_json, write_json
from recovery_core.preview import preview
from recovery_core.service import _validate_candidates, extract_candidate, recover, scan
from recovery_core.verification import verify
from recovery_core.windows import VolumeSource


def protect(raw, signature, usa):
    value = bytearray(raw)
    value[:4] = signature
    count = len(value) // 512 + 1
    struct.pack_into('<HH', value, 4, usa, count)
    value[usa:usa + 2] = b'\x6a\x91'
    for i in range(1, count):
        value[usa + i * 2:usa + i * 2 + 2] = value[i * 512 - 2:i * 512]
        value[i * 512 - 2:i * 512] = b'\x6a\x91'
    return bytes(value)


def resident(kind, value):
    attr = bytearray((24 + len(value) + 7) // 8 * 8)
    struct.pack_into('<II', attr, 0, kind, len(attr))
    struct.pack_into('<IH', attr, 16, len(value), 24)
    attr[24:24 + len(value)] = value
    return bytes(attr)


def filename(name, parent=5, parent_seq=7):
    encoded = name.encode('utf-16le')
    value = bytearray(66 + len(encoded))
    struct.pack_into('<Q', value, 0, parent | parent_seq << 48)
    value[64:66] = bytes((len(encoded) // 2, 1))
    value[66:] = encoded
    return resident(0x30, value)


def nonresident(mapping, size, initialized=None):
    pairs, previous = bytearray(), 0
    for lcn, count in mapping:
        pairs += bytes((0x21, count)) + (lcn - previous).to_bytes(2, 'little', signed=True)
        previous = lcn
    pairs += b'\0'
    attr = bytearray((64 + len(pairs) + 7) // 8 * 8)
    struct.pack_into('<II', attr, 0, 0x80, len(attr))
    attr[8] = 1
    struct.pack_into('<QQH', attr, 16, 0, (sum(c for _, c in mapping) - 1) % (1 << 64), 64)
    struct.pack_into('<QQQ', attr, 40, sum(c for _, c in mapping) * 4096, size, size if initialized is None else initialized)
    attr[64:64 + len(pairs)] = pairs
    return bytes(attr)


def file_record(number, sequence, attrs, flags=1, *, used_only=False):
    value = bytearray(1024)
    struct.pack_into('<HHHHII', value, 16, sequence, 1, 56, flags, 56 + sum(map(len, attrs)) + 8, 1024)
    struct.pack_into('<I', value, 44, number)
    position = 56
    for attr in attrs:
        value[position:position + len(attr)] = attr
        position += len(attr)
    value[position:position + 4] = b'\xff' * 4
    value = protect(value, b'FILE', 48)
    return ntfs_log.fixup(value, b'FILE')[:position + 8] if used_only else value


class LogWriter:
    """Serialize pages, sector protection and cross-page records independently."""
    def __init__(self):
        self.raw = bytearray(64 * 4096)
        self.base, self.position = 4 * 4096, 64
        self.page_meta = {}
        self.offsets = []
        self.previous = 0

    def append(self, op, data=b'', old=b'', *, undo=0, number=29, roff=0, aoff=0, count=None, tx=24):
        if self.position + 48 > 4096:
            self.base += 4096
            self.position = 64
        start = self.base + self.position
        lsn = (2 << 16) + start // 8
        client = struct.pack('<12HQ', op, undo, 40, len(data) if count is None else count,
                             40 + len(data), len(old), 24, 0 if op == 27 else 1, roff, aoff, (number % 4) * 2, 2, number // 4)
        client += struct.pack('<Q', 4 + number // 4) + data + old
        header = struct.pack('<QQQIIIIH6x', lsn, self.previous, self.previous, len(client), 0, 1, tx,
                             1 if self.position + 48 + len(client) > 4096 else 0)
        pending = header + client
        while pending:
            take = min(4096 - self.position, len(pending))
            self.raw[self.base + self.position:self.base + self.position + take] = pending[:take]
            self.position += take
            pending = pending[take:]
            meta = self.page_meta.setdefault(self.base, [0, 64, 0])
            meta[0] = lsn
            if pending:
                self.base += 4096
                self.position = 64
            else:
                self.position = (self.position + 7) // 8 * 8
                meta[1:3] = [self.position, lsn]
        self.previous = 0 if op == 27 else lsn
        self.offsets.append(start)
        return lsn

    def finish(self):
        for base, (last, next_offset, last_end) in self.page_meta.items():
            struct.pack_into('<Q', self.raw, base + 8, last)
            struct.pack_into('<IHHH', self.raw, base + 16, 1, 1, 1, next_offset)
            struct.pack_into('<Q', self.raw, base + 32, last_end)
            self.raw[base:base + 4096] = protect(self.raw[base:base + 4096], b'RCRD', 40)
        for base in (0, 4096):
            page = bytearray(4096)
            struct.pack_into('<IIHHH', page, 16, 4096, 4096, 48, 1, 1)
            struct.pack_into('<QHHHHIHHQIHH', page, 48, (2 << 16) + self.base // 8, 1, 65535, 0, 0,
                             48, 160, 64, len(self.raw), 0, 48, 64)
            struct.pack_into('<I', page, 112 + 28, 8)
            page[144:152] = 'NTFS'.encode('utf-16le')
            self.raw[base:base + 4096] = protect(page, b'RSTR', 30)
        return bytes(self.raw)


class MetadataBackend:
    versions = {'fls': 'independent-log-fixture', 'icat': 'independent-log-fixture'}

    def __init__(self, mft, log, bitmap):
        self.metadata, self.bitmap = {0: mft, 2: log}, bitmap
        self.calls = []

    def scan(self, image, offset, sector):
        return [], []

    def extract_log_metadata(self, image, offset, sector, record, output, limit):
        self.calls.append(record)
        if len(self.metadata[record]) > limit:
            raise RecoveryError('metadata budget exceeded')
        output.write(self.metadata[record])

    def extract_bitmap(self, image, offset, sector, output, limit):
        output.write(self.bitmap)


def sample(root, *, allocated=(), mapping=((80, 2),), resident_data=False, cross_page=False,
           close=True, rollback=False, current_seq=5, current_flags=1, initialized=None,
           missing_parent=False, malformed_update=False, mapping_updates=False, wrong_size_undo=False):
    original = ''.join(f'{i:04d} independent text line: 检查字节\n' for i in range(170)).encode('utf-8')
    assert 4096 < len(original) < 8192
    data_attr = nonresident(mapping, len(original), initialized)
    name_attr = filename('证据.txt', 23, 2)
    initial_data = resident(0x80, b'') if resident_data or mapping_updates else data_attr
    init = file_record(29, 4, [name_attr, initial_data], used_only=True)
    mft = bytearray(64 * 1024)
    records = {0: file_record(0, 1, [nonresident(((4, 16),), len(mft))]),
               5: file_record(5, 7, [], flags=3),
               20: file_record(20, 3, [filename('Documents')], flags=3),
               23: file_record(23, 3, [filename('reused')], flags=3),
               29: file_record(29, current_seq, [filename('other.bin'), resident(0x80, b'other')], flags=current_flags)}
    for number, value in records.items():
        mft[number * 1024:(number + 1) * 1024] = value
    writer = LogWriter()
    if not missing_parent:
        writer.append(2, file_record(23, 2, [filename('deleted-folder', 20, 3)], flags=3, used_only=True), number=23)
        writer.append(27, undo=1)
    if cross_page:
        # Force the initialization header to finish exactly at a sector/page
        # boundary; its actual client payload starts in the next log page.
        writer.position = 4048
    writer.append(2, init)
    attr_offset = 56 + len(name_attr)
    if mapping_updates:
        writer.append(6, old=initial_data, undo=5, roff=attr_offset)
        writer.append(37, roff=attr_offset + 8, count=1024 - attr_offset - 8)
        empty = nonresident((), 0)
        empty = empty + bytes(len(data_attr) - len(empty))
        empty = bytearray(empty)
        struct.pack_into('<I', empty, 4, len(empty))
        writer.append(5, bytes(empty), undo=6, roff=attr_offset)
        writer.append(9, data_attr[64:], bytes(empty)[64:], undo=9, roff=attr_offset, aoff=64)
        writer.append(11, struct.pack('<QQQ', 8192, len(original), len(original)), bytes(24), undo=11, roff=attr_offset)
    if malformed_update:
        writer.append(9, b'\xff', b'\0', undo=9, roff=attr_offset, aoff=64)
    if wrong_size_undo:
        writer.append(11, struct.pack('<QQQ', 8192, 5000, 5000), bytes(24), undo=11, roff=attr_offset)
    if rollback:
        writer.append(7, b'\0' * 8, undo=1, roff=attr_offset, aoff=24)
    if close:
        writer.append(27, undo=1)
    log = writer.finish()
    image = bytearray(3 * 512 + 256 * 4096)
    start = 3 * 512
    image[start + 3:start + 11] = b'NTFS    '
    struct.pack_into('<HB', image, start + 11, 512, 8)
    struct.pack_into('<Q', image, start + 40, 256 * 8)
    image[start + 64] = 246
    image[start + 510:start + 512] = b'\x55\xaa'
    image[start + 4 * 4096:start + 20 * 4096] = mft
    logical = 0
    for lcn, count in mapping:
        length = min(count * 4096, len(original) - logical)
        image[start + lcn * 4096:start + lcn * 4096 + length] = original[logical:logical + length]
        logical += length
    bitmap = bytearray(32)
    for cluster in [*range(20), *allocated]:
        bitmap[cluster // 8] |= 1 << (cluster % 8)
    path = root / 'independent.img'
    path.write_bytes(image)
    return path, MetadataBackend(bytes(mft), log, bytes(bitmap)), original, writer


class NtfsLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def start(self, **options):
        self.image, self.backend, self.original, self.writer = sample(self.root, **options)
        self.session = self.root / 'scan'
        return scan(self.image, self.session, backend=self.backend, offset=3, deep_log=True)

    def test_scan_preview_save_reopen_and_independent_original_verification(self):
        item, = self.start()['candidates']
        before = self.image.read_bytes()
        self.assertEqual(item['original_path'], '/Documents/deleted-folder/证据.txt')
        self.assertEqual(preview(self.session, item['id'], self.backend)['text'], self.original.decode())
        output = self.root / 'out'
        report = recover(self.session, output, backend=self.backend)
        saved, = report['results']
        self.assertEqual(saved['status'], 'exported_unverified')
        self.assertEqual((output / saved['saved_path']).read_bytes(), self.original)
        self.assertEqual(report['source_verification'], 'unchanged')
        self.assertEqual(self.image.read_bytes(), before)
        _validate_candidates(read_json(self.session / 'session.json')['candidates'])
        reference = self.root / 'truth.json'
        write_json(reference, {'schema_version': 1, 'files': [{'original_path': item['original_path'],
            'size': len(self.original), 'sha256': hashlib.sha256(self.original).hexdigest()}]})
        verified = verify(output, reference)
        self.assertEqual((verified['exact_content_matches'], verified['correct_paths']), (1, 1))
        self.assertEqual(set(self.backend.calls), {0, 2})

    def test_first_cluster_overwrite_is_a_fragment_never_a_complete_export(self):
        item, = self.start(allocated=(80,))['candidates']
        self.assertEqual(item['content_status'], 'fragment')
        self.assertEqual(item['ntfs_log']['file_offset'], 4096)
        self.assertEqual(item['size'], len(self.original) - 4096)
        result = preview(self.session, item['id'], self.backend)
        self.assertIn('不完整片段', result['note'])
        output = self.root / 'out'
        saved, = recover(self.session, output, backend=self.backend)['results']
        self.assertEqual(saved['status'], 'partial')
        self.assertIn('.fragment-00001000.txt', saved['saved_path'])
        self.assertEqual((output / saved['saved_path']).read_bytes(), self.original[4096:])
        reference = self.root / 'truth.json'
        write_json(reference, {'schema_version': 1, 'files': [{'original_path': item['original_path'],
            'size': len(self.original), 'sha256': hashlib.sha256(self.original).hexdigest()}]})
        self.assertEqual(verify(output, reference)['exact_content_matches'], 0)

    def test_signed_run_deltas_reassemble_noncontiguous_extents(self):
        item, = self.start(mapping=((80, 1), (72, 1)))['candidates']
        self.assertEqual(len(item['ntfs_log']['extents']), 2)
        data = io.BytesIO()
        extract_candidate(self.image, 3, 512, item, data, backend=self.backend)
        self.assertEqual(data.getvalue(), self.original)

    def test_cross_page_client_payload_and_sector_fixups(self):
        item, = self.start(cross_page=True)['candidates']
        self.assertEqual(item['ntfs_log']['sha256'], hashlib.sha256(self.original).hexdigest())

    def test_attribute_conversion_zero_tail_mapping_and_size_updates(self):
        item, = self.start(mapping_updates=True)['candidates']
        self.assertEqual(item['size'], len(self.original))
        self.assertGreater(len(item['ntfs_log']['evidence_lsns']), 3)

    def test_uninitialized_tail_is_not_zero_padded_or_counted_complete(self):
        item, = self.start(initialized=5012)['candidates']
        self.assertEqual((item['size'], item['content_status']), (5012, 'fragment'))

    def test_allocated_middle_cluster_splits_logical_ranges(self):
        from recovery_core.carving import Geometry
        volume = Geometry(1536, 4096, 256)
        bitmap = bytearray(32)
        bitmap[81 // 8] = 1 << (81 % 8)
        groups = ntfs_log.available_ranges([(0, 80, 3)], 11000, 11000, volume, bitmap)
        self.assertEqual([[ (e['file_offset'], e['length']) for e in g] for g in groups],
                         [[(0, 4096)], [(8192, 2808)]])

    def test_size_update_cannot_bridge_a_missing_prior_size_change(self):
        self.assertEqual(self.start(wrong_size_undo=True)['candidates'], [])

    def test_known_fragment_cannot_count_as_another_smaller_complete_file(self):
        self.start(allocated=(80,))
        destination = self.root / 'out'
        recover(self.session, destination, backend=self.backend)
        reference = self.root / 'smaller-file.json'
        remainder = self.original[4096:]
        write_json(reference, {'schema_version': 1, 'files': [{'original_path': '/other.txt',
            'size': len(remainder), 'sha256': hashlib.sha256(remainder).hexdigest()}]})
        checked = verify(destination, reference)
        self.assertEqual(checked['exact_content_matches'], 0)
        self.assertEqual(checked['outputs'][0]['excluded_reason'], 'known_fragment')

    def test_copy_time_change_is_detected_even_after_descriptor_revalidation(self):
        item, = self.start()['candidates']
        with patch.object(ntfs_log, 'scan_log', return_value=([{
                k: v for k, v in item.items() if k != 'id'}], {})):
            with self.image.open('r+b') as stream:
                stream.seek(item['ntfs_log']['extents'][0]['image_offset'])
                stream.write(b'x')
            with self.assertRaises(RecoveryError):
                ntfs_log.extract_log(self.image, 3, 512, item, io.BytesIO(), backend=self.backend)

    def test_empty_log_pages_are_not_candidates(self):
        image, backend, _, _ = sample(self.root)
        backend.metadata[2] = LogWriter().finish()
        candidates, report = ntfs_log.scan_log(image, 3, 512, backend=backend)
        self.assertEqual(candidates, [])
        self.assertEqual(report['log_records'], 0)

    def test_record_limit_stops_scan(self):
        with patch.object(ntfs_log, 'MAX_RECORDS', 1), self.assertRaises(RecoveryError):
            self.start()

    def test_forged_descriptor_bounds_fail_before_extraction(self):
        item, = self.start()['candidates']
        for field, value in [('record', True), ('sequence', 65535), ('original_size', -1), ('extents', [])]:
            modified = copy.deepcopy(item)
            modified['ntfs_log'][field] = value
            with self.subTest(field=field), self.assertRaises(RecoveryError):
                _validate_candidates([modified])

    def test_allocated_data_yields_diagnostic_without_export(self):
        report = self.start(allocated=(80, 81))
        self.assertEqual(report['candidates'], [])
        self.assertEqual(report['ntfs_log']['diagnostics'][0]['status'], 'no_unallocated_data')

    def test_resident_initialization_is_not_mistaken_for_empty_original(self):
        report = self.start(resident_data=True)
        self.assertEqual(report['candidates'], [])
        self.assertIn('Resident data', report['ntfs_log']['diagnostics'][0]['reason'])

    def test_live_and_normally_deleted_generations_are_not_extra_candidates(self):
        for sequence, flags in ((4, 1), (5, 0), (3, 1)):
            with self.subTest(sequence=sequence, flags=flags), tempfile.TemporaryDirectory() as root:
                image, backend, _, _ = sample(Path(root), current_seq=sequence, current_flags=flags)
                candidates, _ = ntfs_log.scan_log(image, 3, 512, backend=backend)
                self.assertEqual(candidates, [])

    def test_incomplete_transaction_never_supplies_initialization(self):
        self.assertEqual(self.start(close=False)['candidates'], [])

    def test_rollback_chain_is_rejected(self):
        self.assertEqual(self.start(rollback=True)['candidates'], [])

    def test_invalid_later_mapping_does_not_reuse_earlier_valid_snapshot(self):
        report = self.start(malformed_update=True)
        self.assertEqual(report['candidates'], [])
        self.assertIn('Mapping', report['ntfs_log']['diagnostics'][0]['reason'])

    def test_missing_parent_does_not_invent_original_directory(self):
        item, = self.start(missing_parent=True)['candidates']
        self.assertIsNone(item['original_path'])
        self.assertEqual(item['path_evidence'], 'historical_name_only')

    def test_torn_cross_page_record_is_rejected(self):
        image, backend, _, _ = sample(self.root, cross_page=True)
        log = bytearray(backend.metadata[2])
        log[5 * 4096 + 510] ^= 1
        backend.metadata[2] = bytes(log)
        candidates, report = ntfs_log.scan_log(image, 3, 512, backend=backend)
        self.assertEqual(candidates, [])
        self.assertGreater(report['rejected_records_or_pages'], 0)

    def test_bad_restart_and_log_version_are_explicit_errors(self):
        image, backend, _, _ = sample(self.root)
        backend.metadata[2] = bytes(len(backend.metadata[2]))
        with self.assertRaises(RecoveryError):
            ntfs_log.scan_log(image, 3, 512, backend=backend)

    def test_truncated_mft_is_an_explicit_error(self):
        image, backend, _, _ = sample(self.root)
        backend.metadata[0] = backend.metadata[0][:-1]
        with self.assertRaises(RecoveryError):
            ntfs_log.scan_log(image, 3, 512, backend=backend)

    def test_opt_in_only_and_live_source_refused_before_access(self):
        image, backend, _, _ = sample(self.root)
        report = scan(image, self.root / 'ordinary', backend=backend, offset=3)
        self.assertEqual(report['candidates'], [])
        self.assertEqual(backend.calls, [])
        with patch('recovery_core.service.volume_identity') as identity, self.assertRaises(RecoveryError):
            scan(VolumeSource('Z:'), self.root / 'live', backend=backend, deep_log=True)
        identity.assert_not_called()

    def test_candidate_limit_does_not_publish_completed_session(self):
        image, backend, _, _ = sample(self.root)
        with self.assertRaises(RecoveryError):
            ntfs_log.scan_log(image, 3, 512, backend=backend, max_candidates=0)

    def test_hash_budget_is_enforced(self):
        with patch.object(ntfs_log, 'MAX_HASH_BYTES', 1), self.assertRaises(RecoveryError):
            self.start()
        self.assertFalse((self.root / 'scan/session.json').exists())

    def test_cancel_does_not_publish_completed_session(self):
        with patch.object(ntfs_log, 'checkpoint', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.start()
        self.assertFalse((self.root / 'scan/session.json').exists())

    def test_fragment_cannot_be_relabelled_as_complete(self):
        item, = self.start(allocated=(80,))['candidates']
        item['content_status'] = 'unverified'
        with self.assertRaises(RecoveryError):
            _validate_candidates([item])

    def test_forged_path_extent_hash_or_evidence_rejected_on_reopen(self):
        item, = self.start()['candidates']
        for kind in ('path', 'extent', 'digest', 'evidence'):
            modified = copy.deepcopy(item)
            if kind == 'path':
                modified['original_path'] = '/a/different.txt'
            elif kind == 'extent':
                modified['ntfs_log']['extents'][0]['image_offset'] += 4096
            elif kind == 'digest':
                modified['ntfs_log']['sha256'] = '0' * 64
            else:
                modified['ntfs_log']['evidence_lsns'][-1] += 1
            with self.subTest(kind=kind), self.assertRaises(RecoveryError):
                extract_candidate(self.image, 3, 512, modified, io.BytesIO(), backend=self.backend)

    def test_source_change_before_preview_is_rejected(self):
        item, = self.start()['candidates']
        with self.image.open('r+b') as stream:
            stream.seek(-1, 2)
            stream.write(b'x')
        with self.assertRaises(RecoveryError):
            preview(self.session, item['id'], self.backend)

    def test_allocation_change_after_scan_is_rejected_on_extract(self):
        item, = self.start()['candidates']
        self.backend.bitmap = bytes([255]) * 32
        with self.assertRaises(RecoveryError):
            extract_candidate(self.image, 3, 512, item, io.BytesIO(), backend=self.backend)

    def test_cli_option_reaches_scanner(self):
        image, backend, _, _ = sample(self.root)
        with patch('recovery_core.cli.Tsk', return_value=backend), patch('sys.stdout', new_callable=io.StringIO):
            code = main(['scan', str(image), '--offset', '3', '--output', str(self.root / 'scan'), '--deep-log'])
        self.assertEqual(code, 0)
        self.assertTrue(read_json(self.root / 'scan/session.json')['scan_options']['deep_log'])

    def test_fragment_command_exit_code_is_incomplete(self):
        self.start(allocated=(80,))
        with patch('recovery_core.cli.Tsk', return_value=self.backend), patch('sys.stdout', new_callable=io.StringIO):
            code = main(['recover', str(self.session), '--destination', str(self.root / 'out')])
        self.assertEqual(code, 1)

    def test_runlist_refuses_sparse_negative_overlap_and_unterminated_ranges(self):
        for mapping in (((0, 2),), ((80, 2), (80, 1)), ((255, 2),)):
            with self.subTest(mapping=mapping), self.assertRaises(ntfs_log.InvalidLog):
                ntfs_log.runs(nonresident(mapping, 5000), 256)
        attr = bytearray(nonresident(((80, 2),), 5000))
        attr[64] = 1  # Sparse run (no LCN delta).
        with self.assertRaises(ntfs_log.InvalidLog):
            ntfs_log.runs(attr, 256)

    def test_log_metadata_backend_only_accepts_fixed_records(self):
        from recovery_core.tsk import Tsk
        backend = Tsk.__new__(Tsk)
        for value in ('2', True, 5, '2-128-1'):
            with self.subTest(value=value), self.assertRaises(RecoveryError):
                backend.extract_log_metadata(Path('image'), 0, 512, value, io.BytesIO(), 100)


if __name__ == '__main__':
    unittest.main()
