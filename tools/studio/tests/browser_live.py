import json, sys, time, urllib.request
from playwright.sync_api import sync_playwright
BASE = 'http://127.0.0.1:8951'; CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'
A, B = 'scail_' + 'a' * 32, 'scail_' + 'b' * 32
fails = []
def check(name, cond, extra=''):
    print(('PASS ' if cond else 'FAIL ') + name + ((' :: ' + str(extra)) if not cond and extra != '' else ''))
    if not cond: fails.append(name)
def wait(page, expr, ms=8000):
    end = time.time() + ms / 1000; last = None
    while time.time() < end:
        try:
            last = page.evaluate(expr)
            if last: return last
        except Exception as e: last = str(e)
        time.sleep(0.15)
    raise TimeoutError(f'{expr} -> {last}')
def calls(): return json.load(urllib.request.urlopen(BASE + '/__calls'))
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])
    page = b.new_page(viewport={'width': 1000, 'height': 1300})
    errors, csp = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('console', lambda m: csp.append(m.text) if 'Content Security Policy' in m.text else None)
    page.on('dialog', lambda d: d.accept())
    page.goto(BASE + '/scail-live?id=' + B, wait_until='domcontentloaded')
    wait(page, "document.querySelectorAll('.job').length === 2")
    check('two job cards rendered under CSP', True); check('no CSP violations', not csp, csp)
    check('running job listed first', page.locator('.job').first.get_attribute('data-id') == A)
    check('?id= highlights that job', 'focus' in (page.locator(f'.job[data-id="{B}"]').get_attribute('class') or ''))
    ja = page.locator(f'.job[data-id="{A}"]')
    check('chunk strip: 2 done, 1 current, 2 waiting', ja.locator('.seg.done').count() == 2 and ja.locator('.seg.cur').count() == 1 and ja.locator('.seg.pend').count() == 2)
    check('current chunk shows live percent', '50%' in ja.locator('.seg.cur').text_content())
    check('running job: stop button, no resume', ja.locator('.btn.stop').count() == 1 and ja.locator('button:has-text("이어서 생성")').count() == 0)
    jb = page.locator(f'.job[data-id="{B}"]')
    check('stopped job: resume buttons, no stop', jb.locator('button:has-text("이어서 생성")').count() == 1 and jb.locator('.btn.stop').count() == 0)
    check('player hidden at first', not page.locator('#playerCard').is_visible())

    ja.locator('.seg.done').first.click()
    wait(page, "!document.querySelector('#playerCard').classList.contains('show') === false")
    src1 = page.evaluate("document.querySelector('#player').currentSrc")
    check('click on a done chunk plays that part', '/v/a.mp4' in src1 and '1구간만' in page.text_content('#plWhat'), (src1, page.text_content('#plWhat')))
    check('played chunk is highlighted', ja.locator('.seg.on').count() == 1)
    wait(page, "document.querySelector('#player').readyState >= 2")
    ja.locator('button:has-text("지금까지 이어보기")').click()
    wait(page, "document.querySelector('#player').currentSrc.includes('/preview/2.mp4')")
    check('joined preview points at /preview/2.mp4', True)
    check('joined preview highlights no single chunk', ja.locator('.seg.on').count() == 0)

    # a new chunk finishes while the video is playing: card updates, the player is untouched
    wait(page, "document.querySelector('#player').readyState >= 2")
    page.evaluate("document.querySelector('#player').currentTime = 0.5")
    before = page.evaluate("[document.querySelector('#player').currentSrc, document.querySelector('#player').currentTime]")
    urllib.request.urlopen(BASE + '/__advance').read()
    wait(page, f"document.querySelectorAll('.job[data-id=\"{A}\"] .seg.done').length === 3", 9000)
    after = page.evaluate("[document.querySelector('#player').currentSrc, document.querySelector('#player').currentTime]")
    check('new chunk appears within a poll', True)
    check('player was not reloaded by the refresh', after[0] == before[0] and after[1] >= before[1] - 0.01, (before, after))
    check('card text reflects 3 done', '3/5 구간 완료' in ja.text_content())

    ja.locator('.btn.stop').click()
    wait(page, f"document.querySelector('.job[data-id=\"{A}\"] .chip').textContent.includes('중단됨')", 9000)
    check('stop sends the cancel call', ('cancel', A) in [tuple(c) for c in calls()], calls())
    check('after stop: resume offered', ja.locator('button:has-text("이어서 생성")').count() == 1)
    ja.locator('button:has-text("이어서 생성")').click()
    wait(page, f"document.querySelector('.job[data-id=\"{A}\"] .chip').textContent.includes('생성 중')", 9000)
    check('resume (front) sends where=front', ('resume', A, 'front') in [tuple(c) for c in calls()], calls())
    jb.locator('button:has-text("맨 뒤로")').click()
    wait(page, f"document.querySelector('.job[data-id=\"{B}\"] .chip').textContent.includes('대기 중')", 9000)
    check('resume (back) sends where=back', ('resume', B, 'back') in [tuple(c) for c in calls()], calls())
    check('closing the player empties and hides it', (page.click('#plClose') or True) and not page.locator('#playerCard').is_visible()
          and page.evaluate("!document.querySelector('#player').getAttribute('src')"))
    check('no page errors', not errors, errors)
    page.screenshot(path='shot_live.png', full_page=True)

    urllib.request.urlopen(BASE + '/__empty').read()
    wait(page, "document.querySelector('#jobs .empty') !== null", 9000)
    check('empty state is friendly', '없어요' in page.text_content('#jobs'))

    m = b.new_page(viewport={'width': 390, 'height': 844}); S = None
    m.goto(BASE + '/scail-live', wait_until='domcontentloaded')
    check('phone: no horizontal overflow', not m.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1'))
    b.close()
print('\nFAILED: ' + ', '.join(fails) if fails else '\nALL LIVE-PAGE CHECKS PASSED')
sys.exit(1 if fails else 0)
