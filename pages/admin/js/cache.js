// ===== 图片转述缓存管理 =====

var cc_offset = 0,
    cc_total = 0,
    cc_pending = null,
    cc_search = '',
    cc_allItems = [];
var CC_PAGE = 20;

// 字节数 → 人类可读体积
function ccFmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

// 时间戳（秒） → YYYY-MM-DD HH:mm
function ccFmtDate(ts) {
  if (!ts) return '—';
  var d = new Date(ts * 1000),
      pad = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function ccEscapeHtml(s) {
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ---- 确认弹窗 / 灯箱 ----
function cacheCloseModal() {
  document.getElementById('ccConfirmModal').classList.remove('show');
  cc_pending = null;
}

function cacheModalConfirm() {
  if (cc_pending) { cc_pending(); cc_pending = null; }
  cacheCloseModal();
}

function cacheCloseLightbox() {
  document.getElementById('ccLightbox').classList.remove('show');
  document.getElementById('ccLightboxImg').src = '';
}

function cacheOpenLightbox(url) {
  document.getElementById('ccLightboxImg').src = url;
  document.getElementById('ccLightbox').classList.add('show');
}

// ---- 统计（缓存条数 + 数据库大小）----
function cacheLoadStats() {
  var B = window.AstrBotPluginPage;
  if (!B) return;
  B.apiGet('caption_cache/stats').then(function (r) {
    var d = r && r.data ? r.data : r;
    document.getElementById('ccStatCount').textContent = d.count != null ? d.count.toLocaleString() : '0';
    document.getElementById('ccStatSize').textContent = d.db_size != null ? ccFmtSize(d.db_size) : '0 B';
    cc_total = d.count || 0;
    document.getElementById('btnClearAll').disabled = cc_total === 0;
  }).catch(function () {
    document.getElementById('ccStatCount').textContent = '—';
    document.getElementById('ccStatSize').textContent = '—';
  });
}

// ---- 渲染单条缓存项 ----
function cacheRenderItem(item) {
  var el = document.createElement('div');
  el.className = 'cache-item';
  el.dataset.hash = item.img_hash;

  var pe = document.createElement('div');
  pe.className = 'img-preview';
  if (item.image_url && (item.image_url.startsWith('http') || item.image_url.startsWith('/'))) {
    pe.onclick = function () { cacheOpenLightbox(item.image_url); };
    var img = document.createElement('img');
    img.src = item.image_url;
    img.loading = 'lazy';
    img.onerror = function () { pe.innerHTML = '<div class="no-img">🖼️</div>'; };
    pe.appendChild(img);
  } else {
    pe.innerHTML = '<div class="no-img">🖼️</div>';
  }
  el.appendChild(pe);

  var hashShort = item.img_hash.length > 12 ? item.img_hash.slice(0, 12) + '…' : item.img_hash;
  var ce = document.createElement('div');
  ce.className = 'item-content';
  ce.innerHTML = '<div class="item-caption" data-hash="' + item.img_hash + '" onclick="cacheStartEdit(this)">' + ccEscapeHtml(item.caption || '(无转述内容)') + '</div><div class="item-meta"><span class="hash" title="' + item.img_hash + '">' + hashShort + '</span><span>📅 ' + ccFmtDate(item.created_at) + '</span></div>';
  el.appendChild(ce);

  var ae = document.createElement('div');
  ae.className = 'item-actions';
  ae.innerHTML = '<button class="btn btn-danger btn-sm" onclick="cacheDeleteItem(\'' + item.img_hash + '\',this)">删除</button>';
  el.appendChild(ae);

  return el;
}

// ---- 删除单条 ----
function cacheDeleteItem(hash, btnEl) {
  document.getElementById('ccConfirmModal').classList.add('show');
  document.getElementById('ccModalTitle').textContent = '确认删除';
  document.getElementById('ccModalDesc').textContent = '确定要删除这条缓存记录吗？';
  cc_pending = function () {
    var B = window.AstrBotPluginPage;
    B.apiPost('caption_cache/delete', { img_hash: hash }).then(function () {
      var el = btnEl.closest('.cache-item');
      if (el) {
        el.classList.add('removing');
        setTimeout(function () { el.remove(); }, 400);
      }
      showToast('已删除');
      cacheLoadStats();
    }).catch(function (e) { showToast('删除失败: ' + e.message); });
  };
}

// ---- 行内编辑转述文本 ----
function cacheStartEdit(captionEl) {
  if (captionEl.classList.contains('editing')) return;
  var hash = captionEl.dataset.hash,
      currentText = captionEl.textContent;
  captionEl.classList.add('editing');
  var ea = document.createElement('div');
  ea.className = 'edit-area';
  ea.innerHTML = '<textarea>' + ccEscapeHtml(currentText === '(无转述内容)' ? '' : currentText) + '</textarea><div class="edit-actions"><button class="btn btn-cancel btn-sm" onclick="cacheCancelEdit(this)">取消</button><button class="btn btn-primary btn-sm" onclick="cacheSaveEdit(this,\'' + hash + '\')">✓ 保存</button></div>';
  captionEl.style.display = 'none';
  captionEl.parentElement.insertBefore(ea, captionEl.nextSibling);
  var ta = ea.querySelector('textarea');
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function cacheCancelEdit(btn) {
  var ea = btn.closest('.edit-area'),
      cel = ea.previousElementSibling;
  cel.style.display = '';
  cel.classList.remove('editing');
  ea.remove();
}

function cacheSaveEdit(btn, hash) {
  var ea = btn.closest('.edit-area'),
      ta = ea.querySelector('textarea'),
      nc = ta.value.trim(),
      cel = ea.previousElementSibling;
  btn.disabled = true;
  btn.textContent = '保存中…';
  window.AstrBotPluginPage.apiPost('caption_cache/update', { img_hash: hash, caption: nc }).then(function () {
    cel.textContent = nc || '(无转述内容)';
    cel.style.display = '';
    cel.classList.remove('editing');
    ea.remove();
    showToast('已保存');
  }).catch(function (e) {
    showToast('保存失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = '✓ 保存';
  });
}

// ---- 一键清空 ----
function cacheConfirmClearAll() {
  document.getElementById('ccConfirmModal').classList.add('show');
  document.getElementById('ccModalTitle').textContent = '清空所有缓存';
  document.getElementById('ccModalDesc').textContent = '确定要清空全部 ' + cc_total + ' 条缓存记录吗？';
  cc_pending = function () {
    var B = window.AstrBotPluginPage;
    B.apiPost('caption_cache/clear').then(function (r) {
      var d = r && r.deleted != null ? r.deleted : 0;
      showToast('已清空 ' + d + ' 条缓存');
      cc_offset = 0;
      cc_allItems = [];
      cacheLoadItems();
      cacheLoadStats();
    }).catch(function (e) { showToast('清空失败: ' + e.message); });
  };
}

// ---- 按时间清理 ----
function cacheConfirmClearBefore() {
  var val = document.getElementById('dateBefore').value;
  if (!val) { showToast('请先选择日期'); return; }
  var ts = new Date(val + 'T23:59:59').getTime() / 1000;
  document.getElementById('ccConfirmModal').classList.add('show');
  document.getElementById('ccModalTitle').textContent = '按时间清理缓存';
  document.getElementById('ccModalDesc').textContent = '确定要删除 ' + val + ' 之前的所有缓存吗？';
  cc_pending = function () {
    var B = window.AstrBotPluginPage;
    B.apiPost('caption_cache/clear_before', { before: ts }).then(function (r) {
      var d = r && r.deleted != null ? r.deleted : 0;
      showToast('已清理 ' + d + ' 条缓存');
      cc_offset = 0;
      cc_allItems = [];
      cacheLoadItems();
      cacheLoadStats();
    }).catch(function (e) { showToast('清理失败: ' + e.message); });
  };
}

// ---- 分页加载列表 ----
function cacheLoadItems(append) {
  append = append || false;
  var B = window.AstrBotPluginPage;
  if (!B) return;
  var params = { offset: cc_offset, limit: CC_PAGE };
  if (cc_search) params.search = cc_search;
  var list = document.getElementById('ccCacheList'),
      lm = document.getElementById('ccLoadMore'),
      ce = document.getElementById('ccCacheEmpty');
  B.apiGet('caption_cache', params).then(function (r) {
    var d = r && r.data ? r.data : r,
        items = d.items || [];
    cc_total = d.total || 0;
    if (!append) list.innerHTML = '';
    if (cc_total === 0 && !append) {
      ce.style.display = '';
      lm.style.display = 'none';
      return;
    } else {
      ce.style.display = 'none';
    }
    items.forEach(function (i) {
      var el = cacheRenderItem(i);
      list.appendChild(el);
      requestAnimationFrame(function () { el.classList.add('visible'); });
    });
    cc_allItems = append ? cc_allItems.concat(items) : items;
    cc_offset += items.length;
    if (cc_offset >= cc_total) { lm.style.display = 'none'; }
    else { lm.style.display = ''; }
  }).catch(function (e) {
    list.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><div>加载失败</div></div>';
  });
}

function cacheLoadMore() {
  var b = document.getElementById('ccBtnLoadMore');
  b.disabled = true;
  b.textContent = '加载中…';
  cacheLoadItems(true);
  setTimeout(function () { b.disabled = false; b.textContent = '加载更多'; }, 500);
}

function cacheClearSearch() {
  document.getElementById('ccSearchInput').value = '';
  document.getElementById('ccSearchClear').classList.remove('show');
  if (cc_search) {
    cc_search = '';
    cc_offset = 0;
    cc_allItems = [];
    cacheLoadItems();
  }
  document.getElementById('ccSearchInput').focus();
}
