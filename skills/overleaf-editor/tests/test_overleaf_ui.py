"""Offline safety checks; no browser or credentials required."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('overleaf_ui', Path(__file__).parents[1] / 'scripts' / 'overleaf_ui.py')
assert SPEC is not None and SPEC.loader is not None
u = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(u)


def plan():
    return {'project_url': 'https://www.overleaf.com/project/' + '0' * 24,
            'file_name': 'main.tex', 'expected_before': '% red bird\n',
            'edits': [{'anchor': 'red', 'replacement': 'blue', 'mode': 'replace'}]}


class Plans(unittest.TestCase):
    def test_replace(self):
        self.assertEqual(u.validate_plan(plan()), '% blue bird\n')

    def test_before_after_delete_and_sequential_anchors(self):
        p = plan()
        p['edits'] = [{'anchor': 'bird', 'replacement': 'quiet ', 'mode': 'before'},
                      {'anchor': 'quiet', 'replacement': ' little', 'mode': 'after'},
                      {'anchor': 'red ', 'replacement': '', 'mode': 'replace'}]
        self.assertEqual(u.validate_plan(p), '% quiet little bird\n')

    def test_empty_missing_and_overlapping_anchors(self):
        for text, anchor in [('abc', ''), ('abc', 'z'), ('aaa', 'aa')]:
            with self.subTest(text=text, anchor=anchor), self.assertRaises(u.StopEdit):
                u.planned_edit(text, {'anchor': anchor, 'replacement': '', 'mode': 'replace'})

    def test_multiline_anchor_is_rejected_before_connecting(self):
        p = plan()
        p['edits'][0]['anchor'] = 'bird\n'
        with patch.object(u, 'connect_page') as connect:
            result = u.run(p, apply=True)
        self.assertIn('error', result)
        connect.assert_not_called()

    def test_strict_schema_and_control_characters(self):
        for change in [{'unexpected': True}, {'project_url': 'https://evil.example/project/' + '0' * 24},
                       {'edits': []}, {'expected_before': 7}]:
            p = plan(); p.update(change)
            with self.subTest(change=change), self.assertRaises(u.StopEdit):
                u.validate_plan(p)
        for field in ['anchor', 'replacement']:
            p = plan(); p['edits'][0][field] = '\t'
            with self.assertRaises(u.StopEdit): u.validate_plan(p)

    def test_literal_project_id_not_repaired(self):
        p = plan(); p['project_url'] += ' '
        with self.assertRaises(u.StopEdit): u.validate_plan(p)

    def test_utf16_selection_and_bounds(self):
        text = 'a\U0001f600b'
        u.verify_selection({'start': 1, 'end': 3, 'selected': '\U0001f600'}, text, 1, 2)
        for state in [{'start': 1, 'end': 2, 'selected': '\U0001f600'},
                      {'start': 1, 'end': 3, 'selected': 'x'}]:
            with self.assertRaises(u.StopEdit): u.verify_selection(state, text, 1, 2)
        with self.assertRaises(u.StopEdit): u.verify_selection({}, text, -1, 1)

    def test_websocket_boundary(self):
        self.assertEqual(u.websocket_url('ws://127.0.0.1:9333/devtools/page/test', 9333),
                         'ws://127.0.0.1:9333/devtools/page/test')
        for address in ['ws://evil.example:9333/devtools/page/test',
                        'ws://user@localhost:9333/devtools/page/test',
                        'ws://localhost:9334/devtools/page/test',
                        'ws://localhost:9333/devtools/page/test?q=1']:
            with self.assertRaises(u.StopEdit): u.websocket_url(address, 9333)


class Runtime(unittest.TestCase):
    def test_dry_run_sends_no_input(self):
        with patch.object(u, 'connect_page') as connect, patch.object(u, 'checked_state') as check, \
             patch.object(u, 'select_anchor') as select, patch.object(u, 'type_text') as typing:
            result = u.run(plan())
        self.assertNotIn('error', result)
        self.assertEqual(result['completed_edits'], 0)
        self.assertFalse(result['mutation_started'])
        select.assert_not_called(); typing.assert_not_called(); check.assert_called_once()
        connect.return_value.ws.close.assert_called_once()

    def test_stale_document_cannot_mutate(self):
        with patch.object(u, 'connect_page'), patch.object(u, 'checked_state', side_effect=u.StopEdit('stale')), \
             patch.object(u, 'select_anchor') as select, patch.object(u, 'type_text') as typing:
            result = u.run(plan(), apply=True)
        self.assertIn('error', result)
        self.assertFalse(result['mutation_started'])
        select.assert_not_called(); typing.assert_not_called()

    def test_wrong_selection_stops_before_typing(self):
        p = plan()
        with patch.object(u, 'connect_page'), patch.object(u, 'select_anchor'), \
             patch.object(u, 'checked_state', return_value={'focused': True, 'start': 0, 'end': 3, 'selected': '% r'}), \
             patch.object(u, 'type_text') as typing:
            result = u.run(p, apply=True)
        self.assertIn('error', result)
        typing.assert_not_called()
        self.assertEqual(result['completed_edits'], 0)

    def test_key_events(self):
        from unittest.mock import Mock
        cdp = Mock()
        u.press(cdp, 'ArrowLeft', 'ArrowLeft', 37)
        self.assertEqual([x.kwargs['type'] for x in cdp.call.call_args_list], ['rawKeyDown', 'keyUp'])
        self.assertTrue(all(x.kwargs['windowsVirtualKeyCode'] == 37 for x in cdp.call.call_args_list))
        cdp.reset_mock(); u.type_text(cdp, 'a\n')
        self.assertEqual([x.kwargs['type'] for x in cdp.call.call_args_list], ['char', 'rawKeyDown', 'keyUp'])


if __name__ == '__main__':
    unittest.main()
