// ===== 配置表单：加载 / 保存 / 人设 / 白名单 / 关键词 =====

// 动态添加一条「群组概率覆盖」卡片
function addGroupItem(data) {
  data = data || { group_id: '', reply_probability: null, active_probability: null };
  var list = document.getElementById('groups-list'),
      card = document.createElement('div');
  card.className = 'list-item-card';
  card.innerHTML = '<button class="remove-btn" onclick="this.parentElement.remove()">&times;</button><div class="list-grid"><div class="form-group"><label>群号 / UMO</label><input type="text" class="text-input group-id" value="' + (data.group_id || '') + '" placeholder="例如 12345678"></div><div class="form-group"><label>独立回复概率 (%) (留空使用全局)</label><input type="number" class="text-input group-reply" value="' + (data.reply_probability !== null ? data.reply_probability : '') + '" placeholder="全局/默认" min="0" max="100"></div><div class="form-group"><label>独立主动概率 (%) (留空使用全局)</label><input type="number" class="text-input group-active" value="' + (data.active_probability !== null ? data.active_probability : '') + '" placeholder="全局/默认" min="0" max="100"></div></div>';
  list.appendChild(card);
}

// 动态添加一条「兴趣关键词规则」卡片
function addKeywordItem(data) {
  data = data || { groups: '', keywords: '', probability: null };
  var list = document.getElementById('keywords-list'),
      card = document.createElement('div');
  card.className = 'list-item-card';
  card.innerHTML = '<button class="remove-btn" onclick="this.parentElement.remove()">&times;</button><div class="list-grid"><div class="form-group" style="grid-column:span 2"><label>匹配关键字 (逗号分隔)</label><input type="text" class="text-input kw-keywords" value="' + (data.keywords || '') + '" placeholder="例如: 原神,星铁"></div><div class="form-group"><label>生效群聊 (留空=全部)</label><input type="text" class="text-input kw-groups" value="' + (data.groups || '') + '" placeholder="群ID,多个用逗号隔开"></div><div class="form-group"><label>独立触发概率 (%)</label><input type="number" class="text-input kw-probability" value="' + (data.probability !== null ? data.probability : '') + '" placeholder="默认" min="0" max="100"></div></div>';
  list.appendChild(card);
}

// 拉取人格、人设提示词、配置、供应商，并填充整个表单
function loadData() {
  document.getElementById('btnSave').disabled = true;
  var B = window.AstrBotPluginPage;
  if (!B) return;
  Promise.all([
    B.apiGet('personas'),
    B.apiGet('persona_prompts'),
    B.apiGet('config'),
    B.apiGet('providers')
  ]).then(function (r) {
    personas = parseResponse(r[0]) || [];
    personaPrompts = parseResponse(r[1]) || [];
    config = parseResponse(r[2]) || {};
    providers = parseResponse(r[3]) || [];

    var ps = document.getElementById('personaSelect');
    ps.innerHTML = '';
    if (personas.length === 0) {
      ps.innerHTML = '<option value="">未找到系统人格角色</option>';
    } else {
      personas.forEach(function (p) {
        var o = document.createElement('option');
        o.value = p.id || p.name;
        o.textContent = p.name + ' (' + (p.id || '') + ')';
        ps.appendChild(o);
      });
      onPersonaChange();
    }

    populateProviders('analyzer_provider_id', config.analyzer_provider_id);
    populateProviders('generator_provider_id', config.generator_provider_id);
    populateProviders('image_caption_provider_id', config.image_caption_provider_id);

    document.getElementById('trigger_mode').value = config.trigger_mode || 'llm_response';
    document.getElementById('filter_prefixes').value = (config.filter_prefixes || []).join(', ');

    // 数值型输入（滑块）
    ['reply_probability', 'track_timeout_seconds', 'max_detection_count', 'active_probability',
     'max_proactive_rounds', 'proactive_cooldown_seconds', 'keyword_default_probability',
     'image_caption_probability', 'silence_multiplier', 'min_silence_seconds', 'max_silence_seconds',
     'max_batch_wait_seconds', 'max_batch_messages', 'caption_timeout_seconds', 'wake_at_threshold',
     'wake_window_minutes', 'typing_delay_min', 'typing_delay_max'].forEach(function (k) {
      var el = document.getElementById(k);
      if (el && config[k] !== undefined) el.value = config[k];
    });

    // 开关型输入（复选）
    ['enable_llm_tools', 'enable_keyword_trigger', 'enable_keyword_on_image', 'enable_image_caption',
     'batch_analysis_enabled', 'instant_at_bot', 'human_like_mode'].forEach(function (k) {
      var el = document.getElementById(k);
      if (el && config[k] !== undefined) el.checked = !!config[k];
    });

    document.getElementById('caption_timeout_behavior').value = config.caption_timeout_behavior || 'wait_then_fallback';
    document.getElementById('analyzer_system_prompt').value = config.analyzer_system_prompt || '';
    document.getElementById('proactive_analyzer_system_prompt').value = config.proactive_analyzer_system_prompt || '';

    document.getElementById('groups-list').innerHTML = '';
    (config.enabled_groups || []).forEach(function (g) { addGroupItem(g); });
    document.getElementById('keywords-list').innerHTML = '';
    (config.keyword_rules || []).forEach(function (k) { addKeywordItem(k); });

    initSliders();
    document.getElementById('btnSave').disabled = false;
    showToast('配置数据加载成功');
  }).catch(function (e) {
    showToast('数据获取失败: ' + e.message);
  });
}

// 切换所选人格 → 同步显示其专属人设提示词
function onPersonaChange() {
  var v = document.getElementById('personaSelect').value;
  currentPersonaName = v;
  if (!v) {
    document.getElementById('personaPrompt').value = '';
    return;
  }
  var f = personaPrompts.find(function (i) {
    return i.persona_name.toLowerCase() === v.toLowerCase();
  });
  document.getElementById('personaPrompt').value = f ? (f.custom_persona_prompt || '') : '';
}

// 从 DOM 收集「群组概率覆盖」列表
function getGroupWhitelistData() {
  var list = [];
  document.querySelectorAll('#groups-list .list-item-card').forEach(function (c) {
    var gid = c.querySelector('.group-id').value.trim();
    if (!gid) return;
    var rp = c.querySelector('.group-reply').value,
        ap = c.querySelector('.group-active').value;
    list.push({
      group_id: gid,
      reply_probability: rp === '' ? null : parseInt(rp),
      active_probability: ap === '' ? null : parseInt(ap),
      __template_key: "group_template"
    });
  });
  return list;
}

// 从 DOM 收集「兴趣关键词规则」列表
function getKeywordRulesData() {
  var list = [];
  document.querySelectorAll('#keywords-list .list-item-card').forEach(function (c) {
    var kw = c.querySelector('.kw-keywords').value.trim();
    if (!kw) return;
    var g = c.querySelector('.kw-groups').value.trim(),
        p = c.querySelector('.kw-probability').value;
    list.push({
      groups: g,
      keywords: kw,
      probability: p === '' ? null : parseInt(p),
      __template_key: "keyword_template"
    });
  });
  return list;
}

// 收集整个表单并保存（先存人设提示词，再存配置）
function saveAll() {
  var btnSave = document.getElementById('btnSave');
  btnSave.disabled = true;
  btnSave.textContent = '保存中...';
  var B = window.AstrBotPluginPage;
  if (!B) return;

  var u = {};
  for (var k in config) u[k] = config[k];

  u.trigger_mode = document.getElementById('trigger_mode').value;
  u.filter_prefixes = document.getElementById('filter_prefixes').value
    .split(',').map(function (s) { return s.trim(); }).filter(Boolean);
  u.analyzer_provider_id = document.getElementById('analyzer_provider_id').value;
  u.generator_provider_id = document.getElementById('generator_provider_id').value;
  u.image_caption_provider_id = document.getElementById('image_caption_provider_id').value;
  u.caption_timeout_behavior = document.getElementById('caption_timeout_behavior').value;
  u.analyzer_system_prompt = document.getElementById('analyzer_system_prompt').value;
  u.proactive_analyzer_system_prompt = document.getElementById('proactive_analyzer_system_prompt').value;

  // 整数型字段
  ['reply_probability', 'track_timeout_seconds', 'max_detection_count', 'active_probability',
   'max_proactive_rounds', 'proactive_cooldown_seconds', 'keyword_default_probability',
   'image_caption_probability', 'min_silence_seconds', 'max_silence_seconds', 'max_batch_wait_seconds',
   'max_batch_messages', 'caption_timeout_seconds', 'wake_at_threshold', 'wake_window_minutes'].forEach(function (k) {
    u[k] = parseInt(document.getElementById(k).value);
  });

  // 浮点型字段
  ['silence_multiplier', 'typing_delay_min', 'typing_delay_max'].forEach(function (k) {
    u[k] = parseFloat(document.getElementById(k).value);
  });

  // 开关型字段
  ['enable_llm_tools', 'enable_keyword_trigger', 'enable_keyword_on_image', 'enable_image_caption',
   'batch_analysis_enabled', 'instant_at_bot', 'human_like_mode'].forEach(function (k) {
    u[k] = document.getElementById(k).checked;
  });

  u.enabled_groups = getGroupWhitelistData();
  u.keyword_rules = getKeywordRulesData();

  var p1 = Promise.resolve();
  if (currentPersonaName) {
    var cv = document.getElementById('personaPrompt').value;
    p1 = B.apiPost('persona_prompts', {
      persona_name: currentPersonaName,
      custom_persona_prompt: cv
    }).then(function (pr) {
      if (!pr || pr.status !== 'ok') throw new Error('更新人设提示词失败: ' + (pr ? pr.message : '未知错误'));
      var idx = personaPrompts.findIndex(function (i) {
        return i.persona_name.toLowerCase() === currentPersonaName.toLowerCase();
      });
      if (idx !== -1) {
        if (cv.trim() === '') personaPrompts.splice(idx, 1);
        else personaPrompts[idx].custom_persona_prompt = cv;
      } else if (cv.trim() !== '') {
        personaPrompts.push({ persona_name: currentPersonaName, custom_persona_prompt: cv });
      }
    });
  }

  p1.then(function () {
    return B.apiPost('config', u);
  }).then(function (rc) {
    if (rc && rc.status === 'ok') {
      config = u;
      showToast('已保存');
    } else {
      throw new Error(rc ? rc.message : '保存失败');
    }
  }).catch(function (e) {
    showToast('保存错误: ' + e.message);
  }).finally(function () {
    btnSave.disabled = false;
    btnSave.textContent = '💾 保存并生效';
  });
}
