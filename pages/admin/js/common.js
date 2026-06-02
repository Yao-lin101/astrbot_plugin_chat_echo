// ===== 共享全局状态 =====
// 这些变量被 config.js / stats.js / cache.js 跨文件共享。
// common.js 最先加载，确保下游脚本使用前已声明。
var toastTimer = null;
var config = {};
var personas = [];
var personaPrompts = [];
var providers = [];
var currentPersonaName = '';

// ===== 主题：跟随系统深色 / 浅色 =====
(function () {
  const m = window.matchMedia('(prefers-color-scheme:dark)');
  function s(dark) {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  }
  s(m.matches);
  m.addEventListener('change', e => s(e.matches));
})();

// ===== 轻提示 Toast =====
function showToast(msg, dur) {
  dur = dur || 2500;
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    t.classList.remove('show');
  }, dur);
}

// ===== 标签页切换 =====
// 仅「设置类」标签需要底部保存栏；用量统计 / 缓存管理属于查看类，无需保存。
var SETTINGS_TABS = ['persona', 'general', 'reply', 'keyword', 'batch', 'human'];

function applyFooterForTab(tabName) {
  var footer = document.querySelector('.footer-bar');
  if (footer) footer.style.display = SETTINGS_TABS.indexOf(tabName) >= 0 ? 'flex' : 'none';
}

function switchTab(tabName) {
  document.querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
  document.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.remove('active'); });
  var btn = Array.from(document.querySelectorAll('.tab-btn')).find(function (b) {
    return b.getAttribute('onclick').includes("'" + tabName + "'");
  });
  if (btn) btn.classList.add('active');
  var panel = document.getElementById('tab-' + tabName);
  if (panel) panel.classList.add('active');
  // 设置类标签才显示底部保存栏
  applyFooterForTab(tabName);
  // 切到缓存管理时懒加载统计与列表
  if (tabName === 'cache') {
    cc_offset = 0;
    cc_allItems = [];
    cacheLoadStats();
    cacheLoadItems();
  }
}

// ===== 滑块数值显示 =====
function updateSliderValue(el, isFloat, unit) {
  var ind = document.getElementById('val-' + el.id);
  if (!ind) return;
  var v = el.value;
  if (el.id.indexOf('probability') >= 0) ind.textContent = v + '%';
  else if (el.id.indexOf('seconds') >= 0 || el.id.indexOf('delay') >= 0) ind.textContent = v + 's';
  else if (el.id.indexOf('minutes') >= 0) ind.textContent = v + 'm';
  else ind.textContent = v + (unit || '');
}

function initSliders() {
  document.querySelectorAll('.slider-input').forEach(function (s) { updateSliderValue(s); });
}

// ===== 通用工具 =====
// 后端约定：成功返回 {status:'ok', data:...}；这里统一解包出 data。
function parseResponse(r) {
  return r && r.status === 'ok' && r.data !== undefined ? r.data : r;
}

// 用 providers 列表填充某个 <select>
function populateProviders(sid, cur) {
  var s = document.getElementById(sid);
  s.innerHTML = '<option value="">使用会话默认 / 留空</option>';
  providers.forEach(function (p) {
    var o = document.createElement('option');
    o.value = p.id;
    o.textContent = p.name;
    if (p.id === cur) o.selected = true;
    s.appendChild(o);
  });
}
