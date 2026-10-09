import json
import sys
import urllib.request
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8941'
CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'
fails = []

import time


def wait_js(page, expr, timeout=8000):
    """Poll with page.evaluate (CDP, exempt from the page CSP) instead of wait_for_function (needs eval)."""
    end = time.time() + timeout / 1000
    last = None
    while time.time() < end:
        try:
            last = page.evaluate(expr)
            if last:
                return last
        except Exception as e:
            last = str(e)
        time.sleep(0.15)
    raise TimeoutError(f'{expr} -> {last}')


def wait_posts(n, timeout=6000):
    end = time.time() + timeout / 1000
    while time.time() < end:
        if len(json.load(urllib.request.urlopen(BASE + '/__posts'))) >= n:
            return
        time.sleep(0.15)
    raise TimeoutError(f'posts >= {n}')



def check(name, cond, extra=''):
    print(('PASS ' if cond else 'FAIL ') + name + ((' :: ' + str(extra)) if not cond and extra != '' else ''))
    if not cond:
        fails.append(name)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])
    page = b.new_page(viewport={'width': 1100, 'height': 1400})
    errors, csp = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: csp.append(m.text) if 'Content Security Policy' in m.text else None)

    page.goto(BASE + '/scail-repair', wait_until='domcontentloaded')
    page.wait_for_selector('.cand', timeout=8000)
    check('candidate list rendered under CSP', page.locator('.cand').count() == 2)
    check('no CSP violations', not csp, csp)

    page.locator('.cand').first.click()
    wait_js(page, 'state.dur > 0')
    check('editor opened and video loaded', page.locator('#editor').is_visible())
    wait_js(page, "document.querySelector('#jobInfo').textContent.includes('8스텝')")
    check('job defaults shown', '8스텝' in page.text_content('#jobInfo'))

    # saved versions panel
    wait_js(page, "document.querySelectorAll('#versions .ver').length === 2")
    check('saved versions listed (newest first)', page.locator('#versions .ver').first.text_content().startswith('수정본 2'))
    check('current version tagged', '현재 기준' in page.locator('#versions .ver').first.text_content())
    check('raw files are linkable', page.locator('#versions .ver a[href="/v/b.mp4"]').count() >= 1)
    check('export button on both undelivered versions', page.locator('#versions button:has-text("내보내기")').count() == 2)
    page.locator('#versions .ver').nth(1).locator('button').click()
    wait_js(page, "document.querySelector('#versions').textContent.includes('내보내는 중')")
    dl = json.load(urllib.request.urlopen(BASE + '/__delivers'))
    check('export POST carries the version', len(dl) == 1 and dl[0]['version'] == '1', dl)
    check('row shows exporting state', '내보내는 중' in page.locator('#versions .ver').nth(1).text_content())
    wait_js(page, "document.querySelectorAll('#versions a.good').length >= 1", 14000)
    check('after export the graded link appears and the button is gone',
          '음악·후보정' in page.locator('#versions .ver').nth(1).text_content()
          and page.locator('#versions .ver').nth(1).locator('button').count() == 0)

    # preset -> controls
    page.click('#adv summary')
    page.click('[data-preset="tiny"]')
    check('tiny preset sets 352x640', page.input_value('#optRes') == '352x640')
    check('low-res warning visible', page.locator('#resWarn').is_visible())
    page.click('[data-preset="hands"]')
    check('hands preset sets 10 steps + DPO on', page.input_value('#optSteps') == '10' and page.input_value('#optDpo') == '1')
    page.click('[data-preset="tiny"]')
    page.select_option('#optDpo', '1')
    page.fill('#optShift', '3')
    page.fill('#seed', '77')
    page.fill('#note', '왼손 손가락')

    # reference image: capture a frame
    page.evaluate("document.querySelector('#vid').currentTime = 1.0")
    wait_js(page, "document.querySelector('#vid').readyState >= 2")
    page.wait_for_timeout(300)
    page.click('#refCapBtn')
    wait_js(page, 'state.refBlob && state.refBlob.size > 0')
    check('frame captured as reference', page.locator('#refPrev').is_visible())
    page.check('#refScail')

    page.click('#submit')
    page.wait_for_selector('.compare video', timeout=15000)
    posts = json.load(urllib.request.urlopen(BASE + '/__posts'))
    check('one POST received', len(posts) == 1, len(posts))
    f = posts[0]
    o = json.loads(f['options'])
    check('options sent', o.get('width') == 352 and o.get('height') == 640 and o.get('steps') == 6 and o.get('tail') == 4
          and o.get('dpo') is True and o.get('shift') == 3 and o.get('ref_to_scail') is True, o)
    check('seed + note sent', f['seed'] == '77' and f['note'] == '왼손 손가락', f)
    check('reference file uploaded', isinstance(f.get('reference'), dict) and f['reference']['size'] > 1000, f.get('reference'))
    check('preempt flag sent', f['preempt'] == 'true')

    check('result shows 3 videos (before / fixed / segment-only)', page.locator('.compare video').count() == 3)
    check('extend controls present', page.locator('#extBefore').is_visible() and page.locator('#extAfter').is_visible())
    page.screenshot(path='shot_result.png', full_page=True)

    # extend forward: continues right after the repaired range, reusing text/seed/options
    first, last = page.evaluate('[state.last.first, state.last.last]')
    page.fill('#extAmt', '0.25')
    page.click('#extAfter')
    wait_posts(2)
    posts = json.load(urllib.request.urlopen(BASE + '/__posts'))
    check('extend-after POST received', len(posts) == 2, len(posts))
    e = posts[1]
    check('forward extension starts exactly after the repaired range',
          abs(float(e['start']) - (last + 1) / 24) < 0.002 and abs(float(e['end']) - float(e['start']) - 0.25) < 0.002, e)
    check('forward extension reuses text, seed and options',
          e.get('reuse') == 'true' and e.get('seed') == '77' and json.loads(e['options']) == o and e.get('addition'), e)
    wait_js(page, "!!state.last && state.last.first === %d" % int(last + 1), 15000)

    # extend backward
    first2 = page.evaluate('state.last.first')
    page.click('#extBefore')
    wait_posts(3)
    posts = json.load(urllib.request.urlopen(BASE + '/__posts'))
    check('extend-before POST received', len(posts) == 3, len(posts))
    g = posts[2]
    check('backward extension ends exactly where the repaired range starts',
          abs(float(g['end']) - first2 / 24) < 0.002 and abs(float(g['end']) - float(g['start']) - 0.25) < 0.002, g)

    # server-side validation messages are shown, not swallowed
    check('no page errors', not errors, errors)

    # mobile layout sanity
    m = b.new_page(viewport={'width': 390, 'height': 844})
    m.goto(BASE + '/scail-repair', wait_until='domcontentloaded')
    m.wait_for_selector('.cand')
    m.locator('.cand').first.click()
    wait_js(m, 'state.dur > 0')
    m.click('#adv summary')
    overflow = m.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1')
    check('no horizontal overflow on phone width', not overflow)
    m.screenshot(path='shot_mobile.png', full_page=True)
    b.close()

print('\nFAILED: ' + ', '.join(fails) if fails else '\nALL BROWSER CHECKS PASSED')
sys.exit(1 if fails else 0)
