"""Local hierarchy UI: node -> persistent model -> learning jobs."""
from browser_guard import BASE, require_demo
require_demo()
from pathlib import Path
import time
MODEL_NAME="구조 검증 피노키오 "+str(int(time.time()))
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE+'/')
    page.wait_for_selector('.whale-card')
    assert 'Ball detection' not in page.locator('#nav').inner_text()
    page.screenshot(path=str(ROOT/'local/geppetto-whales.png'),full_page=True)
    page.locator('.whale-card').first.click()
    page.wait_for_selector('.pinocchio-card')
    assert page.get_by_role('link',name='Dataset',exact=True).count()==1
    page.get_by_role('link',name='Dataset',exact=True).click()
    page.get_by_role('heading',name='Soccer Ball · Stage3 v4').wait_for()
    page.get_by_role('link',name='Model template',exact=True).click()
    page.get_by_role('link',name='피노키오',exact=True).click()
    page.get_by_role('button',name='＋ 피노키오 만들기').click()
    page.locator('#modal input[name=name]').fill(MODEL_NAME)
    page.locator('#modal select[name=template_id]').select_option('ball-p2-v1')
    page.get_by_role('button',name='Confirm',exact=True).click()
    page.get_by_role('heading',name=MODEL_NAME,exact=True).wait_for()
    page.locator('.pinocchio-card').filter(has_text=MODEL_NAME).first.click()
    page.wait_for_selector('.pinocchio-growth')
    assert '목각인형 · 아직 학습 전' in page.locator('main').inner_text()
    pin_url=page.url
    page.get_by_role('link',name='학습 시작',exact=True).click()
    page.wait_for_selector('#wizard-pinocchio')
    selected=page.locator('#wizard-pinocchio').input_value()
    assert selected
    assert page.locator('#wizard-node').input_value()=='whale-local'
    assert page.locator('#wizard-category').is_disabled()
    page.locator('#run-name').fill('피노키오 연결 검증')
    page.locator('#wizard-next').click()
    page.wait_for_selector('#wizard-model')
    assert page.locator('#wizard-model').is_disabled()
    page.locator('#wizard-next').click()
    page.wait_for_selector('#gpu-mode')
    page.locator('#wizard-next').click()
    page.get_by_role('heading',name='피노키오 연결 검증',exact=True).wait_for()
    assert MODEL_NAME in page.locator('#wizard').inner_text()
    page.locator('#wizard-next').click()
    page.wait_for_selector('#run-chart')
    assert MODEL_NAME in page.locator('#breadcrumb').inner_text()
    page.get_by_role('button',name='Stop training run').click()
    page.locator('#modal input[name=name]').fill('피노키오 연결 검증')
    page.get_by_role('button',name='Confirm',exact=True).click()
    page.wait_for_selector('#modal',state='hidden')
    page.goto(pin_url)
    page.wait_for_selector('.run-table')
    assert '피노키오 연결 검증' in page.locator('.run-table').inner_text()
    routes=['','#node=whale-local','#node=whale-local&tab=datasets','#node=whale-local&tab=models',pin_url.split(BASE+'/')[1],'#page=jobs']
    for width in [320,375,768,1440]:
        page.set_viewport_size({'width':width,'height':1000})
        for route in routes:
            page.goto(BASE+'/'+route)
            page.wait_for_timeout(250)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,route)
    page.goto(BASE+'/#node=whale-local')
    page.wait_for_selector('.pinocchio-card')
    page.screenshot(path=str(ROOT/'local/whale-models.png'),full_page=True)
    assert not errors,errors
    print('PASS: node resources, persistent Pinocchio creation, bound learning job and stop, 6 routes × 4 widths')
    browser.close()
