#!/usr/bin/env python3
"""Guarded local Overleaf edits through an already-open page; dry-run by default."""
import argparse
import json
import re
import sys
import time
import urllib.request
from urllib.parse import urlsplit


class StopEdit(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise StopEdit(message)


def validate_plan(plan):
    require(isinstance(plan, dict), 'plan must be an object')
    require(set(plan) == {'project_url', 'file_name', 'expected_before', 'edits'},
            'unexpected or missing plan fields')
    require(isinstance(plan['project_url'], str) and re.fullmatch(
        r'https://www\.overleaf\.com/project/[a-fA-F0-9]{24}', plan['project_url']),
        'expected https://www.overleaf.com/project/<24 hexadecimal characters>')
    require(isinstance(plan['file_name'], str) and bool(plan['file_name'].strip()),
            'file_name must be nonempty')
    require(isinstance(plan['expected_before'], str), 'expected_before must be text')
    require(isinstance(plan['edits'], list) and bool(plan['edits']), 'edits must be nonempty')
    text = plan['expected_before']
    for edit in plan['edits']:
        require(isinstance(edit, dict) and set(edit) == {'anchor', 'replacement', 'mode'},
                'unexpected or missing edit fields')
        require(isinstance(edit['anchor'], str) and bool(edit['anchor']), 'empty anchor')
        require('\n' not in edit['anchor'], 'multiline anchors are unsupported')
        require(isinstance(edit['replacement'], str), 'replacement must be text')
        require(edit['mode'] in ('replace', 'before', 'after'), 'invalid mode')
        require(all(c in '\n' or ord(c) >= 32 for c in edit['anchor'] + edit['replacement']),
                'control characters are unsupported')
        _, _, text = planned_edit(text, edit)
    return text


def planned_edit(text, edit):
    anchor = edit['anchor']
    require(bool(anchor), 'empty anchor')
    start = text.find(anchor)
    require(start >= 0 and text.find(anchor, start + 1) < 0, 'anchor is not unique')
    end = start + len(anchor)
    mode = edit['mode']
    require(mode in ('replace', 'before', 'after'), 'invalid mode')
    if mode == 'before':
        end = start
    elif mode == 'after':
        start = end
    return start, end, text[:start] + edit['replacement'] + text[end:]


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def verify_selection(state, text, start, end):
    require(0 <= start <= end <= len(text), 'planned range is outside document')
    require(state['start'] == utf16_length(text[:start]) and
            state['end'] == utf16_length(text[:end]) and
            state['selected'] == text[start:end], 'actual selection differs from plan')


def websocket_url(url, port):
    parsed = urlsplit(url)
    require(parsed.scheme == 'ws' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
            and parsed.port == port and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r'/devtools/page/[^/]+', parsed.path),
            'page WebSocket must use the configured loopback port')
    return url


class CDP:
    def __init__(self, url):
        from websockets.sync.client import connect
        self.ws = connect(url, open_timeout=5, close_timeout=1, max_size=16 * 1024 * 1024)
        self.serial = 0
        self.dialog = False
        self.call('Page.enable')

    def call(self, method, **params):
        self.serial += 1
        serial = self.serial
        deadline = time.monotonic() + 5
        self.ws.send(json.dumps({'id': serial, 'method': method, 'params': params}))
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'CDP command timed out')
            msg = json.loads(self.ws.recv(timeout=remaining))
            if msg.get('method') == 'Page.javascriptDialogOpening':
                self.dialog = True
                raise StopEdit('unexpected browser dialog; left untouched')
            if msg.get('id') == serial:
                require('error' not in msg, 'CDP command failed: ' + method)
                return msg.get('result', {})

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', expression=expression, returnByValue=True,
                           timeout=3000)
        require('exceptionDetails' not in result, 'DOM/state read failed')
        value = result.get('result', {}).get('value')
        require(value is not None, 'DOM/state unavailable')
        return value


# shortcut: Overleaf/CodeMirror selectors are version-dependent, revalidate after UI updates.
STATE_JS = """(() => {
 const visible = e => !!(e && e.getClientRects().length);
 if ([...document.querySelectorAll('[role="dialog"],.modal.show,.modal.in')].some(visible))
   throw Error('dialog open');
 const tabs = [...document.querySelectorAll('.editor-file-tab.tab-selected')];
 if (tabs.length !== 1) throw Error('active file ambiguous');
 const names = tabs[0].querySelectorAll('.editor-file-tab-path');
 if (names.length !== 1) throw Error('file-name element unverified');
 const editors = [...document.querySelectorAll('.cm-content')].filter(visible);
 if (editors.length !== 1) throw Error('editor ambiguous');
 const el = editors[0], cv = el.cmView || el.parentElement.cmView;
 if (!cv || !cv.view) throw Error('read-only state unavailable');
 const state = cv.view.state, sel = state.selection.main;
 if (state.selection.ranges.length !== 1) throw Error('multiple selections');
 const rect = el.getBoundingClientRect();
 return {url: location.href, file: names[0].textContent.replace(/^\u200e/, ''),
   text: state.doc.toString(), start: sel.from, end: sel.to,
   selected: state.doc.sliceString(sel.from, sel.to),
   focused: el.contains(document.activeElement),
   x: rect.left + Math.min(rect.width / 2, 40),
   y: rect.top + Math.min(rect.height / 2, 20)};
})()"""

SEARCH_JS = """(() => {
 const inputs = document.querySelectorAll('input[name="search"][aria-label="Find"]');
 if (inputs.length !== 1) throw Error('search field ambiguous');
 const input = inputs[0], form = input.closest('form');
 if (!form) throw Error('search options unverified');
 const checks = [...form.querySelectorAll('input[type="checkbox"]')];
 if (!checks.length) throw Error('search options unavailable');
 return {focused: document.activeElement === input,
         plain: checks.every(c => !c.checked)};
})()"""


def checked_state(cdp, plan, expected):
    state = cdp.evaluate(STATE_JS)
    require(state['url'] == plan['project_url'], 'project URL changed')
    require(state['file'] == plan['file_name'], 'active file differs from plan')
    require(state['text'] == expected, 'document differs from expected text')
    return state


def press(cdp, key, code, virtual_key, modifiers=0):
    for kind in ('rawKeyDown', 'keyUp'):
        cdp.call('Input.dispatchKeyEvent', type=kind, key=key, code=code,
                 windowsVirtualKeyCode=virtual_key, nativeVirtualKeyCode=virtual_key,
                 modifiers=modifiers)


def type_text(cdp, text):
    for char in text:
        if char == '\n':
            press(cdp, 'Enter', 'Enter', 13)
        else:
            cdp.call('Input.dispatchKeyEvent', type='char', text=char)


def select_anchor(cdp, plan, text, edit):
    state = checked_state(cdp, plan, text)
    if not state['focused']:
        for kind in ('mousePressed', 'mouseReleased'):
            cdp.call('Input.dispatchMouseEvent', type=kind, x=state['x'], y=state['y'],
                     button='left', clickCount=1)
    require(checked_state(cdp, plan, text)['focused'], 'editor is not focused')
    press(cdp, 'f', 'KeyF', 70, 2)
    search = cdp.evaluate(SEARCH_JS)
    require(search['focused'] and search['plain'], 'search focus/options unsafe')
    press(cdp, 'a', 'KeyA', 65, 2)
    type_text(cdp, edit['anchor'])
    press(cdp, 'Enter', 'Enter', 13)
    press(cdp, 'Escape', 'Escape', 27)
    start = text.index(edit['anchor'])
    end = start + len(edit['anchor'])
    state = checked_state(cdp, plan, text)
    require(state['focused'], 'search did not return focus to editor')
    verify_selection(state, text, start, end)
    if edit['mode'] != 'replace':
        before = edit['mode'] == 'before'
        press(cdp, 'ArrowLeft' if before else 'ArrowRight',
              'ArrowLeft' if before else 'ArrowRight', 37 if before else 39)
        caret = start if before else end
        verify_selection(checked_state(cdp, plan, text), text, caret, caret)


def connect_page(plan, port):
    require(type(port) is int and 1 <= port <= 65535, 'invalid CDP port')
    # Disable redirects and proxies: discovery must remain on loopback.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise StopEdit('CDP HTTP redirects are forbidden')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open('http://127.0.0.1:%d/json/list' % port, timeout=5) as response:
        targets = json.load(response)
    pages = [t for t in targets if t.get('type') == 'page'
             and t.get('url') == plan['project_url']]
    require(len(pages) == 1, 'expected exactly one matching project page')
    return CDP(websocket_url(pages[0].get('webSocketDebuggerUrl', ''), port))


def run(plan, apply=False, port=9333):
    started = time.monotonic()
    result = {'apply': apply, 'before_match': False, 'after_match': False,
              'completed_edits': 0, 'mutation_started': False, 'current_state': 'not_read'}
    cdp = None
    expected = None
    try:
        final = validate_plan(plan)
        expected = plan['expected_before']
        cdp = connect_page(plan, port)
        checked_state(cdp, plan, expected)
        result.update(before_match=True, current_state='expected_before')
        if apply:
            for edit in plan['edits']:
                _, _, updated = planned_edit(expected, edit)
                select_anchor(cdp, plan, expected, edit)
                # Recheck selection, file and text immediately before mutation.
                state = checked_state(cdp, plan, expected)
                start, end, _ = planned_edit(expected, edit)
                require(state['focused'], 'editor focus lost')
                verify_selection(state, expected, start, end)
                result['mutation_started'] = True
                if edit['replacement']:
                    type_text(cdp, edit['replacement'])
                elif edit['mode'] == 'replace':
                    press(cdp, 'Backspace', 'Backspace', 8)
                checked_state(cdp, plan, updated)
                expected = updated
                result['completed_edits'] += 1
                result['current_state'] = 'last_completed_edit'
            result['after_match'] = checked_state(cdp, plan, final)['text'] == final
        else:
            result['after_match'] = True
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
        result['current_state'] = 'unknown'
        if cdp is not None and not cdp.dialog:
            try:
                state = cdp.evaluate(STATE_JS)
                result['current_state'] = ('last_verified_text' if state['text'] == expected
                                           else 'different_text')
                result['current_file_matches'] = state['file'] == plan['file_name']
                result['current_project_matches'] = state['url'] == plan['project_url']
            except Exception:
                pass
    finally:
        if cdp is not None:
            try:
                cdp.ws.close()
            except Exception:
                result.setdefault('error', 'CDP connection close failed')
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--port', type=int, default=9333)
    args = parser.parse_args()
    try:
        with open(args.plan, encoding='utf-8') as stream:
            plan = json.load(stream)
        result = run(plan, args.apply, args.port)
    except Exception as error:
        result = {'error': type(error).__name__ + ': ' + str(error), 'completed_edits': 0}
    print(json.dumps(result, ensure_ascii=False))
    return 2 if 'error' in result else 0


if __name__ == '__main__':
    sys.exit(main())
