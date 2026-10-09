import json, sys, time, urllib.request
from playwright.sync_api import sync_playwright
BASE = 'http://127.0.0.1:8971'; CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome'
fails = []
def check(name, cond, extra=''):
    print(('PASS ' if cond else 'FAIL ') + name + ((' :: ' + str(extra)) if not cond and extra != '' else ''))
    if not cond: fails.append(name)
def wait(page, expr, ms=9000):
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
    b = p.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox'])
    page = b.new_page(viewport={'width': 900, 'height': 1500}); errs, csp = [], []
    page.on('pageerror', lambda e: errs.append(str(e))); page.on('console', lambda m: csp.append(m.text) if 'Content Security' in m.text else None)
    dialogs = []
    page.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(BASE + '/ops-assistant', wait_until='domcontentloaded')
    wait(page, "document.querySelectorAll('#target option').length === 3")
    check('page loads under the site CSP with the target list', not csp and not errs, (csp, errs))
    page.fill('#prompt', '유튜브 토큰 오류 왜 났어?')
    wait(page, "document.querySelector('#target').value === 'youtube_api.py'")
    check('target is suggested from the wording', '추천' in page.text_content('#suggested'))
    page.click('#ask')
    wait(page, "document.querySelectorAll('.card[data-id]').length === 1")
    c = [x for x in calls() if x[0] == 'create'][0][1]
    check('question sent with kind=ask, the suggested file and logs on', c['kind'] == 'ask' and c['target'] == 'youtube_api.py' and c['include_logs'] == 'true' and c['preempt'] == 'false', c)
    check('card shows it is waiting for the GPU', '대기 중' in page.text_content('.card .chip'))
    wait(page, "document.querySelector('.card .chip').textContent.includes('답변')", 12000)
    wait(page, "document.querySelector('.card .reply')")
    check('answer is shown as TEXT (markup is not interpreted)', '<img src=x' in page.text_content('.card .reply') and page.locator('.card img').count() == 0)
    check('model-supplied script/handlers never ran', page.evaluate("window.__pwned") is None)

    page.fill('#prompt', '토스트 시간을 4초로 늘려줘')
    page.select_option('#target', 'studio_static/scail-repair.js')
    page.click('#fix')
    wait(page, "document.querySelectorAll('.card[data-id]').length === 2")
    wait(page, "document.querySelector('.card .chip').textContent.includes('승인 필요')", 12000)
    wait(page, "document.querySelector('.card pre.diff')")
    check('proposal shows a coloured diff with additions and removals', page.locator('.card pre.diff .a').count() >= 1 and page.locator('.card pre.diff .d').count() >= 1)
    check('checks are listed', page.locator('.card .checks li.ok').count() == 2)
    check('static file: no restart wording, apply/reject offered', '적용하면 바로 반영' in page.text_content('#list .card') and page.locator('#list .card button:has-text("적용")').count() >= 1)
    page.click('.card button:has-text("✅ 적용")')
    wait(page, "document.querySelector('.card .chip').textContent === '적용됨'")
    check('apply posted', any(x[0] == 'apply' for x in calls()))
    check('no restart banner for a static change', page.text_content('#banner').strip() == '')
    page.click('.card button:has-text("되돌리기")')
    wait(page, "document.querySelector('.card .chip').textContent === '되돌림'")
    check('undo posted', any(x[0] == 'undo' for x in calls()))

    page.fill('#prompt', '구간 수정 서버 상수 추가해줘')
    page.select_option('#target', 'scail_repair.py')
    page.click('#fix')
    wait(page, "document.querySelectorAll('.card[data-id]').length === 3")
    wait(page, "document.querySelector('.card .chip').textContent.includes('승인 필요')", 12000)
    check('server code: restart wording and the risk flag are shown', '재시작 필요' in page.text_content('#list .card') and '파일 삭제' in page.text_content('#list .card .flag'))
    page.click('.card button:has-text("✅ 적용")')
    wait(page, "document.querySelector('#banner').textContent.includes('1건')")
    check('applied server change raises the restart banner', '1건' in page.text_content('#banner') and page.locator('#banner button:has-text("재시작")').count() == 1)
    page.click('#banner button:has-text("재시작")')
    wait(page, "document.querySelector('#banner').textContent.includes('재시작하고 있어요')", 9000)
    check('busy GPU asks for confirmation, then restarts', len(dialogs) == 1 and '영상 생성' in dialogs[0] and [x[0] for x in calls()].count('restart') == 1
          and ('restart-refused',) in [tuple(x) for x in calls()], (dialogs, calls()))
    check('the confirmed restart carried confirm_busy', [x for x in calls() if x[0] == 'restart'][0][1].get('confirm_busy') == 'true')
    check('no page errors or CSP violations in the whole run', not errs and not csp, (errs, csp))
    page.screenshot(path='shot_ops.png', full_page=True)
    m = b.new_page(viewport={'width': 390, 'height': 844}); m.goto(BASE + '/ops-assistant', wait_until='domcontentloaded'); time.sleep(1)
    check('phone: no horizontal overflow', not m.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1'))
    b.close()
print('\nFAILED: ' + ', '.join(fails) if fails else '\nALL ASSISTANT-PAGE CHECKS PASSED'); sys.exit(1 if fails else 0)
