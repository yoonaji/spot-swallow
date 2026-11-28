// 시연 녹화: Grafana 대시보드를 띄워 두고, scripts/demo.py 가 쓰는 data/demo/stage.json 을 보며 화면 위에 자막을 바꾼다.
//
//   node scripts/record_demo.js &          # 먼저 녹화 시작 (stage.json 의 end 가 true 가 되면 끝)
//   python3 scripts/demo.py
//   → data/demo/raw/*.webm (실제 시간), 끝나면 scripts/encode_demo.py 로 1분짜리 docs/demo/demo.mp4 를 만든다.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const STAGE = 'data/demo/stage.json';
const URL = process.env.GRAFANA_URL ||
  'http://localhost:3000/d/spot-swallow/spot-swallow?orgId=1&refresh=5s&from=now-5m&to=now&kiosk';
const TARGET_SECONDS = Number(process.env.TARGET_SECONDS || 60);
const OUT = process.env.OUT || 'docs/demo/demo.mp4';

function readStage() {
  try { return JSON.parse(fs.readFileSync(STAGE, 'utf8')); } catch { return null; }
}

(async () => {
  fs.mkdirSync('data/demo/raw', { recursive: true });
  try { fs.unlinkSync(STAGE); } catch {}
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1600, height: 900 }, colorScheme: 'dark',
    recordVideo: { dir: 'data/demo/raw', size: { width: 1600, height: 900 } },
  });
  const page = await context.newPage();
  const started = Date.now();   // 녹화는 페이지를 만들 때 시작한다
  await page.goto(URL, { waitUntil: 'networkidle' }).catch(() => {});
  await page.evaluate(() => {
    const box = document.createElement('div');
    box.id = 'demo-caption';
    box.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99999;padding:18px 28px;' +
      'background:rgba(10,12,16,.88);color:#fff;font:600 30px/1.3 "Noto Sans CJK KR","Noto Sans KR",sans-serif;' +
      'border-top:4px solid #257de0';
    box.innerHTML = '<div id="demo-title">시작 대기</div><div id="demo-detail" style="font-weight:400;font-size:21px;opacity:.85;margin-top:6px"></div>';
    document.body.appendChild(box);
  });
  let seq = -1;
  const marks = [];   // [녹화 시작부터 초, 단계 제목]
  for (;;) {
    const s = readStage();
    if (s && s.seq !== seq) {
      seq = s.seq;
      await page.evaluate(({ title, detail }) => {
        document.getElementById('demo-title').textContent = title;
        document.getElementById('demo-detail').textContent = detail;
      }, s).catch(() => {});
      marks.push((Date.now() - started) / 1000);
      console.log(new Date().toISOString(), s.title);
      if (s.end) { await page.waitForTimeout(6000); break; }
    }
    if (Date.now() - started > 15 * 60 * 1000) break;
    await page.waitForTimeout(500);
  }
  const video = page.video();
  await context.close();
  await browser.close();
  const raw = await video.path();
  const secs = (Date.now() - started) / 1000;
  fs.writeFileSync('data/demo/marks.json', JSON.stringify({ marks, end: secs }));
  // 단계마다 속도를 달리해 1분으로 줄인다 (scripts/encode_demo.py)
  execFileSync('python3', ['scripts/encode_demo.py', raw, 'data/demo/marks.json', OUT],
    { stdio: 'inherit', env: { ...process.env, TARGET_SECONDS: String(TARGET_SECONDS) } });
})();
