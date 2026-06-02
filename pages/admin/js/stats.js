// ===== 用量统计：Token 趋势图 + 群组排行 =====

var stats_curPeriod = 'month',
    stats_hiddenSet = new Set(),
    chartDates = [],
    chartMeta = {};
var PALETTE = ['#0071e3', '#ff2d55', '#34c759', '#ff9500', '#5856d6', '#ff6482', '#00c7be',
               '#af52de', '#ffd60a', '#30b0c7', '#ff9f0a', '#bf5af2', '#64d2ff', '#ff453a', '#32d74b'];
var PERIOD_DAYS = { day: 7, yesterday: 7, week: 7, month: 31, year: 365 };

// 大数字缩写：1.2M / 3.4万 / 千分位
function fmt(n) {
  if (n == null) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
  return n.toLocaleString();
}

function fmtFull(n) {
  if (n == null) return '0';
  return n.toLocaleString();
}

// 绘制分群每日 Token 趋势折线图（纯手写 SVG）
function drawChart(allGroups) {
  var chart = document.getElementById('chart'),
      legend = document.getElementById('legend');
  if (!allGroups || !allGroups.length) {
    chart.innerHTML = '<text x="500" y="165" text-anchor="middle" fill="var(--text3)" font-size="14">暂无数据</text>';
    legend.innerHTML = '';
    chartDates = [];
    return;
  }

  var W = 1000, H = 320,
      pad = { top: 16, right: 16, bottom: 36, left: 52 },
      cw = W - pad.left - pad.right,
      ch = H - pad.top - pad.bottom,
      maxVal = 0,
      dateSet = new Set();
  var vis = allGroups.filter(function (g) { return !stats_hiddenSet.has(g.group_id); });
  allGroups.forEach(function (g) {
    g.daily.forEach(function (d) {
      dateSet.add(d.date);
      if (!stats_hiddenSet.has(g.group_id) && d.total > maxVal) maxVal = d.total;
    });
  });
  maxVal = Math.max(maxVal, 1);
  var niceMax = Math.pow(10, Math.ceil(Math.log10(maxVal)));
  if (maxVal <= niceMax / 2) niceMax /= 2;
  var dates = Array.from(dateSet).sort();
  chartDates = dates;

  var dm = {};
  allGroups.forEach(function (g) {
    g.daily.forEach(function (d) {
      if (!dm[d.date]) dm[d.date] = {};
      dm[d.date][g.group_id] = {
        name: g.group_name || g.group_id,
        total: d.total,
        prompt: d.prompt,
        completion: d.completion,
        color: PALETTE[allGroups.indexOf(g) % PALETTE.length]
      };
    });
  });
  chartMeta = { dm: dm, pad: pad, cw: cw, ch: ch, H: H, niceMax: niceMax };

  // 渐变定义
  var svg = '<defs>';
  vis.forEach(function (g, i) {
    var c = PALETTE[i % PALETTE.length];
    svg += '<linearGradient id="a' + i + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="' + c + '" stop-opacity="0.2"/><stop offset="100%" stop-color="' + c + '" stop-opacity="0.01"/></linearGradient>';
  });
  svg += '</defs>';

  var tc = getComputedStyle(document.documentElement).getPropertyValue('--text3').trim() || '#86868b';

  // 横向网格线 + Y 轴刻度
  for (var i = 0; i <= 4; i++) {
    var v = (niceMax / 4) * i,
        y = pad.top + ch - (v / niceMax) * ch;
    svg += '<line x1="' + pad.left + '" y1="' + y + '" x2="' + (pad.left + cw) + '" y2="' + y + '" stroke="var(--divider)" stroke-width="0.5"/><text x="' + (pad.left - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="' + tc + '" font-size="11">' + fmt(v) + '</text>';
  }

  // X 轴日期标签
  var n = dates.length;
  if (n > 1) {
    var cnt = Math.min(6, n),
        step = Math.max(1, Math.floor(n / (cnt - 1)));
    for (i = 0; i < n; i += step) {
      var d = dates[i],
          x = pad.left + i * cw / (n - 1),
          p = d.split('-');
      svg += '<text x="' + x + '" y="' + (H - 6) + '" text-anchor="middle" fill="' + tc + '" font-size="10">' + p.slice(1).join('/') + '</text>';
    }
  }

  // 每个群组一条平滑折线 + 渐变填充
  vis.forEach(function (g, gi) {
    var c = PALETTE[gi % PALETTE.length],
        pts = g.daily.filter(function (d) { return dates.indexOf(d.date) >= 0; })
                     .sort(function (a, b) { return a.date.localeCompare(b.date); });
    if (!pts.length) return;
    var pd = '', fd = '';
    pts.forEach(function (p, i) {
      var di = dates.indexOf(p.date),
          x = pad.left + (n > 1 ? di * cw / (n - 1) : cw / 2),
          y = pad.top + ch - (p.total / niceMax) * ch;
      if (i === 0) {
        pd += 'M ' + x + ' ' + y;
        fd = 'M ' + x + ' ' + (pad.top + ch);
      } else {
        var px = pad.left + (n > 1 ? dates.indexOf(pts[i - 1].date) * cw / (n - 1) : 0),
            py = pad.top + ch - (pts[i - 1].total / niceMax) * ch;
        pd += ' C ' + (px + (x - px) * 0.4) + ' ' + py + ', ' + (x - (x - px) * 0.4) + ' ' + y + ', ' + x + ' ' + y;
      }
    });
    var lx = pad.left + (n > 1 ? dates.indexOf(pts[pts.length - 1].date) * cw / (n - 1) : cw / 2),
        ly = pad.top + ch - (pts[pts.length - 1].total / niceMax) * ch;
    fd += ' L ' + lx + ' ' + ly + ' L ' + lx + ' ' + (pad.top + ch) + ' Z';
    svg += '<path d="' + fd + '" fill="url(#a' + gi + ')" opacity="0.7"/><path d="' + pd + '" fill="none" stroke="' + c + '" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>';
  });

  svg += '<line id="vline" y1="' + pad.top + '" y2="' + (pad.top + ch) + '" stroke="var(--text3)" stroke-width="1" stroke-dasharray="4,4" style="display:none"/><rect id="hoverZone" x="' + pad.left + '" y="' + pad.top + '" width="' + cw + '" height="' + ch + '" fill="transparent" pointer-events="all"/>';
  chart.innerHTML = svg;

  // 图例（可点击切换隐藏）
  var lh = '';
  allGroups.forEach(function (g, i) {
    var c = PALETTE[i % PALETTE.length],
        h = stats_hiddenSet.has(g.group_id) ? 'hidden' : '',
        gn = g.group_name || g.group_id;
    lh += '<div class="legend-item ' + h + '" data-gid="' + g.group_id + '"><span class="dot" style="background:' + c + '"></span>' + gn + ' - ' + g.group_id + '</div>';
  });
  legend.innerHTML = lh;
  legend.querySelectorAll('.legend-item').forEach(function (el) {
    el.addEventListener('click', function () {
      var id = el.dataset.gid;
      stats_hiddenSet.has(id) ? stats_hiddenSet.delete(id) : stats_hiddenSet.add(id);
      drawChart(allGroups);
    });
  });
}

// 群组 Token 排行榜
function renderRank(groups) {
  var rl = document.getElementById('rankList');
  if (!groups || !groups.length) {
    rl.innerHTML = '<div class="empty-state"><div class="icon">📋</div><div>暂无数据</div></div>';
    return;
  }
  var mv = groups[0].total || 1, h = '';
  groups.forEach(function (g, i) {
    var rn = g.group_name ? g.group_name + ' - ' + g.group_id : (g.group_id || '未知群组');
    h += '<div class="rank-item" style="animation-delay:' + (i * 0.05) + 's"><div class="rank-num">' + (i < 3 ? ['🥇', '🥈', '🥉'][i] : '#' + (i + 1)) + '</div><div class="rank-info"><div class="name">' + rn + '</div><div class="detail">Prompt ' + fmt(g.prompt) + ' · Completion ' + fmt(g.completion) + '</div></div><div class="rank-tokens">' + fmt(g.total) + '</div><div class="rank-bar-bg"><div class="rank-bar-fill" style="width:0%"></div></div></div>';
  });
  rl.innerHTML = h;
  requestAnimationFrame(function () {
    document.querySelectorAll('#tab-stats .rank-item').forEach(function (el, i) {
      el.classList.add('visible');
      var bar = el.querySelector('.rank-bar-fill'),
          pct = groups[i] ? groups[i].total / mv * 100 : 0;
      setTimeout(function () { bar.style.width = pct + '%'; }, 100 + i * 50);
    });
  });
}

// 切换统计周期并刷新顶部汇总、趋势图、排行
function updateStats(p) {
  var B = window.AstrBotPluginPage;
  if (!B) return;
  stats_curPeriod = p;
  document.querySelectorAll('#tab-stats .seg').forEach(function (s) {
    s.classList.toggle('active', s.dataset.period === p);
  });
  B.apiGet('token_stats', { period: p }).then(function (sr) {
    var sd = sr && sr.data ? sr.data : sr;
    if (!sd) throw 'No data';
    var g = sd.global || {}, gr = sd.groups || [];
    document.getElementById('totalNum').textContent = fmtFull(g.total || 0);
    document.getElementById('promptTotal').textContent = fmtFull(g.prompt || 0);
    document.getElementById('compTotal').textContent = fmtFull(g.completion || 0);
    document.getElementById('groupCount').textContent = g.group_count || 0;
    var days = PERIOD_DAYS[p] || 30;
    return B.apiGet('token_history', { days: days }).then(function (hr) {
      var hd = hr && hr.data ? hr.data : hr;
      drawChart((hd && hd.groups) || []);
      renderRank(gr);
    });
  }).catch(function (e) {});
}
