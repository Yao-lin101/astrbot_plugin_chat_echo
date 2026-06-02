// ===== 初始化：事件绑定 + 首屏加载 =====
// 本文件依赖 common/config/stats/cache 中的函数与全局变量，必须最后加载。

document.addEventListener('DOMContentLoaded', function () {
  // 用量统计周期切换按钮
  document.querySelectorAll('#tab-stats .seg').forEach(function (s) {
    s.addEventListener('click', function () { updateStats(s.dataset.period); });
  });

  // 缓存搜索框（输入防抖 350ms）
  var si = document.getElementById('ccSearchInput'),
      sc = document.getElementById('ccSearchClear'),
      st = null;
  si.addEventListener('input', function () {
    var v = si.value.trim();
    sc.classList.toggle('show', v.length > 0);
    clearTimeout(st);
    st = setTimeout(function () {
      if (v === cc_search) return;
      cc_search = v;
      cc_offset = 0;
      cc_allItems = [];
      cacheLoadItems();
    }, 350);
  });
  si.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      si.value = '';
      sc.classList.remove('show');
      if (cc_search) {
        cc_search = '';
        cc_offset = 0;
        cc_allItems = [];
        cacheLoadItems();
      }
    }
  });

  // 全局 Esc：关闭弹窗与灯箱
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      cacheCloseModal();
      cacheCloseLightbox();
    }
  });

  // 首屏加载：配置表单 + 用量统计（默认展示用量统计标签页）
  loadData();
  updateStats('month');
});
