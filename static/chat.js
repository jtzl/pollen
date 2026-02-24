// ==================== State ====================
var curModel = defaultModel;
var config = modelConfigs[curModel];
var ws = null;
var position = 0;
var sessionLength = 0;
var initialSessionLength = 2048;
var maxSessionLength = config.chat.max_session_length;
var tokenCount = 0;
var generatedTokens = 0;
var totalElapsed = 0;
var generating = false;
var forceStop = false;
var messages = [];

// Chat history (multiple conversations)
var conversations = [];
var activeConvId = null;

// Settings
var userTemperature = config.chat.generation_params.temperature || 0.6;
var userTopP = config.chat.generation_params.top_p || 0.9;
var userMaxTokens = config.chat.max_new_tokens || 500;

// Configure marked for safe rendering
if (typeof marked !== 'undefined') {
  marked.setOptions({ breaks: true, gfm: true });
}

// ==================== DOM Refs ====================
var chatContainer = document.getElementById('chat-container');
var messagesEl = document.getElementById('messages');
var welcomeScreen = document.getElementById('welcome-screen');
var typingIndicator = document.getElementById('typing-indicator');
var inputEl = document.getElementById('message-input');
var sendBtn = document.getElementById('send-btn');
var stopBtn = document.getElementById('stop-btn');
var statusDot = document.getElementById('status-dot');
var statusText = document.getElementById('status-text');
var errorBanner = document.getElementById('error-banner');
var errorTextEl = document.getElementById('error-text');
var speedInfo = document.getElementById('speed-info');
var tokenCounter = document.getElementById('token-counter');
var charCount = document.getElementById('char-count');
var chatHistory = document.getElementById('chat-history');
var sidebar = document.getElementById('sidebar');
var sidebarOverlay = document.getElementById('sidebar-overlay');

// ==================== Connection ====================

function setStatus(state) {
  statusDot.className = 'status-dot w-2 h-2 rounded-full';
  statusDot.classList.remove('status-connecting');
  if (state === 'connected') {
    statusDot.classList.add('bg-green-500');
    statusText.textContent = 'Connected';
  } else if (state === 'connecting') {
    statusDot.classList.add('bg-yellow-500', 'status-connecting');
    statusText.textContent = 'Connecting...';
  } else {
    statusDot.classList.add('bg-red-500');
    statusText.textContent = 'Disconnected';
  }
}

function openSession() {
  return new Promise(function(resolve, reject) {
    setStatus('connecting');
    var protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(protocol + location.host + '/api/v2/generate');
    ws.onopen = function() {
      sessionLength = initialSessionLength;
      ws.send(JSON.stringify({
        type: 'open_inference_session',
        model: curModel,
        max_length: sessionLength
      }));
    };
    ws.onmessage = function(event) {
      var resp = JSON.parse(event.data);
      if (resp.ok) { setStatus('connected'); hideError(); resolve(); }
      else { reject(resp.traceback || 'Failed to open session'); }
    };
    ws.onerror = function() { setStatus('error'); reject('WebSocket connection failed'); };
    ws.onclose = function() { if (generating) setStatus('error'); };
  });
}

// ==================== Sidebar ====================

function toggleSidebar() {
  var isOpen = sidebar.classList.contains('sidebar-open');
  if (isOpen) {
    sidebar.classList.remove('sidebar-open');
    sidebar.classList.add('sidebar-closed');
    sidebarOverlay.classList.add('hidden');
  } else {
    sidebar.classList.remove('sidebar-closed');
    sidebar.classList.add('sidebar-open');
    sidebarOverlay.classList.remove('hidden');
  }
}

function renderChatHistory() {
  chatHistory.innerHTML = '';
  for (var i = conversations.length - 1; i >= 0; i--) {
    var conv = conversations[i];
    var item = document.createElement('div');
    item.className = 'chat-history-item' + (conv.id === activeConvId ? ' active' : '');
    item.textContent = conv.title || 'New Chat';
    item.setAttribute('data-id', conv.id);
    item.onclick = (function(id) { return function() { switchConversation(id); }; })(conv.id);
    chatHistory.appendChild(item);
  }
}

function switchConversation(id) {
  saveCurrentConversation();
  activeConvId = id;
  var conv = conversations.find(function(c) { return c.id === id; });
  if (conv) {
    messages = conv.messages.slice();
    rebuildMessages();
  }
  renderChatHistory();
  if (window.innerWidth < 1024) toggleSidebar();
}

function saveCurrentConversation() {
  if (!activeConvId) return;
  var conv = conversations.find(function(c) { return c.id === activeConvId; });
  if (conv) {
    conv.messages = messages.slice();
    if (messages.length > 0) {
      conv.title = messages[0].content.substring(0, 40) + (messages[0].content.length > 40 ? '...' : '');
    }
  }
}

function newChat() {
  if (generating) return;
  saveCurrentConversation();
  var conv = { id: Date.now(), title: 'New Chat', messages: [] };
  conversations.push(conv);
  activeConvId = conv.id;
  messages = [];
  rebuildMessages();
  renderChatHistory();
  if (ws && ws.readyState <= 1) ws.close();
  openSession().catch(function(err) { showError('Connection failed: ' + err); });
  if (window.innerWidth < 1024) toggleSidebar();
}

// ==================== Message Rendering ====================

function formatTime(d) {
  var h = d.getHours(), m = d.getMinutes();
  var ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
}


function fixMissingSpaces(text) {
  // Protect content that should not be modified
  var preserved = [];
  function protect(match) {
    preserved.push(match);
    return "\x00#" + (preserved.length - 1) + "\x00";
  }

  // Protect fenced code blocks
  text = text.replace(/```[\s\S]*?```/g, protect);
  // Protect inline code (backticks)
  text = text.replace(/`[^`]+`/g, protect);
  // Protect URLs
  text = text.replace(/https?:\/\/\S+/g, protect);
  // Protect email addresses
  text = text.replace(/[\w.\-+]+@[\w.\-]+\.\w+/g, protect);
  // Protect file paths (Unix-style)
  text = text.replace(/(?:^|[\s(])([.]{0,2}\/[\w.\/_\-]+)/gm, function(m) {
    preserved.push(m);
    return "\x00#" + (preserved.length - 1) + "\x00";
  });

  // 1. Letter followed by digit: "approximately215" -> "approximately 215"
  text = text.replace(/([a-zA-Z])(\d)/g, "$1 $2");
  // 2. Digit followed by letter: "215petabytes" -> "215 petabytes"
  text = text.replace(/(\d)([a-zA-Z])/g, "$1 $2");
  // 3. Period followed by letter (no space): "storage.Binary" -> "storage. Binary"
  text = text.replace(/\.([A-Za-z])/g, ". $1");
  // 4. Lowercase followed by uppercase: "dataDNA" -> "data DNA"
  text = text.replace(/([a-z])([A-Z])/g, "$1 $2");
  // 5. Closing paren followed by letter: ")using" -> ") using"
  text = text.replace(/\)([a-zA-Z])/g, ") $1");
  // 6. Exclamation/question mark followed by letter: "help!In" -> "help! In"
  text = text.replace(/([!?])([a-zA-Z])/g, "$1 $2");
  // 7. Colon followed by letter (not in URLs, already protected)
  text = text.replace(/([a-zA-Z]):([a-zA-Z0-9])/g, "$1: $2");
  // 8. Comma followed by letter: "however,there" -> "however, there"
  text = text.replace(/,([a-zA-Z])/g, ", $1");

  // Restore protected content
  text = text.replace(/\x00#(\d+)\x00/g, function(_, idx) {
    return preserved[parseInt(idx)];
  });

  return text;
}

function renderMarkdown(text) {
  if (typeof marked !== 'undefined' && text) {
    try {
      text = fixMissingSpaces(text);
      var html = marked.parse(text);
      html = html.replace(/<pre>/g, '<pre><button class="code-copy-btn" onclick="copyCode(this)">Copy</button>');
      return html;
    } catch (e) { /* fall through */ }
  }
  return escapeHtml(text);
}

function escapeHtml(text) {
  var d = document.createElement('div');
  d.textContent = text || '';
  return d.innerHTML;
}

function addMessage(role, content, time) {
  if (welcomeScreen) welcomeScreen.style.display = 'none';
  var ts = time || new Date();
  var isUser = role === 'user';

  var row = document.createElement('div');
  row.className = 'message-row group flex gap-3 msg-animate ' + (isUser ? 'flex-row-reverse' : '');

  var av = document.createElement('div');
  av.className = isUser ? 'avatar-user' : 'avatar-assistant';
  av.innerHTML = isUser
    ? '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>'
    : '<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>';
  row.appendChild(av);

  var col = document.createElement('div');
  col.className = 'flex flex-col max-w-[85%] sm:max-w-[75%] ' + (isUser ? 'items-end' : 'items-start');

  var bubble = document.createElement('div');
  bubble.className = 'relative px-4 py-3 rounded-2xl text-[13.5px] leading-relaxed ' +
    (isUser
      ? 'bg-gradient-to-br from-blue-600 to-accent-600 text-white rounded-br-md'
      : 'bg-panel-200 text-gray-200 border border-white/5 rounded-bl-md');

  var textEl = document.createElement('div');
  textEl.className = 'msg-content';
  if (isUser) {
    textEl.innerHTML = escapeHtml(content);
    textEl.style.whiteSpace = 'pre-wrap';
    textEl.style.wordBreak = 'break-word';
  } else {
    textEl.innerHTML = renderMarkdown(content);
  }
  bubble.appendChild(textEl);
  col.appendChild(bubble);

  var meta = document.createElement('div');
  meta.className = 'flex items-center gap-2 mt-1 px-1';

  var tsEl = document.createElement('span');
  tsEl.className = 'text-[10px] text-gray-600';
  tsEl.textContent = formatTime(ts);
  meta.appendChild(tsEl);

  var actionsEl = document.createElement('div');
  actionsEl.className = 'msg-actions flex items-center gap-1';

  var copyBtn = document.createElement('button');
  copyBtn.className = 'text-[10px] text-gray-500 hover:text-gray-300 transition-colors px-1.5 py-0.5 rounded hover:bg-white/5';
  copyBtn.textContent = 'Copy';
  copyBtn.onclick = function() {
    var raw = textEl.innerText || textEl.textContent;
    navigator.clipboard.writeText(raw).then(function() {
      copyBtn.textContent = 'Copied!';
      setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500);
    });
  };
  actionsEl.appendChild(copyBtn);
  meta.appendChild(actionsEl);
  col.appendChild(meta);

  row.appendChild(col);
  messagesEl.appendChild(row);
  scrollToBottom();
  return textEl;
}

function getLastAssistantContent() {
  var all = messagesEl.querySelectorAll('.message-row:last-child .msg-content');
  return all.length > 0 ? all[all.length - 1] : null;
}

function updateLastAssistant(fullText) {
  var el = getLastAssistantContent();
  if (el) {
    el.innerHTML = renderMarkdown(fullText);
    scrollToBottom();
  }
}

function rebuildMessages() {
  messagesEl.innerHTML = '';
  if (messages.length === 0) {
    if (welcomeScreen) {
      messagesEl.appendChild(welcomeScreen);
      welcomeScreen.style.display = '';
    }
    return;
  }
  if (welcomeScreen) welcomeScreen.style.display = 'none';
  for (var i = 0; i < messages.length; i++) {
    addMessage(messages[i].role, messages[i].content, messages[i].time);
  }
}

function scrollToBottom() {
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

// ==================== Copy code block helper ====================

function copyCode(btn) {
  var pre = btn.parentElement;
  var code = pre.querySelector('code');
  var text = code ? code.textContent : pre.textContent;
  navigator.clipboard.writeText(text).then(function() {
    btn.textContent = 'Copied!';
    setTimeout(function() { btn.textContent = 'Copy'; }, 1500);
  });
}

// ==================== Prompt suggestions ====================

function insertPrompt(text) {
  inputEl.value = text;
  onInputChange(inputEl);
  inputEl.focus();
}

// ==================== Error Handling ====================

function showError(msg) {
  errorTextEl.textContent = msg;
  errorBanner.classList.remove('hidden');
}
function hideError() { errorBanner.classList.add('hidden'); }

function retryConnection() {
  hideError();
  openSession().catch(function(err) { showError('Retry failed: ' + err); });
}

// ==================== Token Counter ====================

function updateTokenCounter() {
  if (tokenCounter) {
    tokenCounter.textContent = generatedTokens + '/' + userMaxTokens;
    tokenCounter.classList.remove('hidden');
  }
}

function hideTokenCounter() {
  if (tokenCounter) {
    tokenCounter.classList.add('hidden');
  }
}

function trimToLastSentence(text) {
  var lastDot = text.lastIndexOf(".");
  var lastBang = text.lastIndexOf("\!");
  var lastQ = text.lastIndexOf("?");
  var lastEnd = Math.max(lastDot, lastBang, lastQ);
  if (lastEnd > 0) return text.substring(0, lastEnd + 1);
  return text;
}

// ==================== Generation Core ====================

function buildPrompt() {
  var sysPrompt = config.chat.system_prompt || '';
  var prompt = '<s> ';
  for (var i = 0; i < messages.length; i++) {
    var msg = messages[i];
    if (msg.role === 'user') {
      prompt += '[INST] ';
      if (i === 0 && sysPrompt) {
        prompt += sysPrompt + '\n\n';
      }
      prompt += msg.content + ' [/INST]';
    } else {
      prompt += msg.content;
      if (i < messages.length - 1) {
        prompt += '</s> ';
      }
    }
  }
  return prompt;
}

function sendMessage() {
  var text = inputEl.value.trim();
  if (!text || generating) return;

  inputEl.value = '';
  onInputChange(inputEl);

  var now = new Date();
  messages.push({ role: 'user', content: text, time: now });
  addMessage('user', text, now);

  messages.push({ role: 'assistant', content: '', time: new Date() });
  addMessage('assistant', '', new Date());

  showTyping();
  setGenerating(true);
  generatedTokens = 0;
  updateTokenCounter();

  var prompt = buildPrompt();

  // Close existing session so a fresh one is opened for the new prompt.
  if (ws && ws.readyState <= 1) {
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
    ws.close();
  }
  ws = null;

  receiveReplica(prompt);

  saveCurrentConversation();
  renderChatHistory();
}

function stopGenerating() {
  var oldWs = ws;
  ws = null;
  if (oldWs && oldWs.readyState <= 1) {
    oldWs.onmessage = null;
    oldWs.onerror = null;
    oldWs.onclose = null;
    oldWs.close();
  }
  forceStop = false;
  setGenerating(false);
  tokenCount = 0; totalElapsed = 0;
  hideTokenCounter();
  enableInput();
  saveCurrentConversation();
  renderChatHistory();
  openSession().catch(function(err) { showError('Reconnection failed: ' + err); });
}

function checkStopSequences(accumulated) {
  var allStopSeqs = [config.chat.stop_token].concat(config.chat.extra_stop_sequences || []);
  for (var j = 0; j < allStopSeqs.length; j++) {
    if (allStopSeqs[j] && accumulated.indexOf(allStopSeqs[j]) >= 0) {
      return accumulated.indexOf(allStopSeqs[j]);
    }
  }
  return -1;
}

function receiveReplica(prompt) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    openSession().then(function() { receiveReplica(prompt); }).catch(function(err) {
      setGenerating(false); hideTyping(); enableInput(); hideTokenCounter();
      showError('Connection failed: ' + err);
    });
    return;
  }

  hideTyping();
  var startTime = Date.now();

  ws.send(JSON.stringify({
    type: 'generate',
    inputs: prompt,
    max_new_tokens: 1,
    max_total_tokens: userMaxTokens,
    stop_sequence: config.chat.stop_token,
    extra_stop_sequences: config.chat.extra_stop_sequences,
    do_sample: config.chat.generation_params.do_sample,
    temperature: userTemperature,
    top_p: userTopP,
    top_k: config.chat.generation_params.top_k || 0,
    repetition_penalty: config.chat.generation_params.repetition_penalty || 1.0
  }));

  ws.onmessage = function(event) {
    var response;
    try { response = JSON.parse(event.data); }
    catch (e) { setGenerating(false); enableInput(); hideTokenCounter(); showError('Invalid response'); return; }

    if (!response.ok) {
      var errMsg = response.traceback || '';
      if (errMsg.indexOf('Maximum length exceeded') >= 0 || errMsg.indexOf('Session expired') >= 0) {
        var newLen = Math.min(sessionLength * 4, maxSessionLength);
        if (newLen > sessionLength) {
          sessionLength = newLen;
          if (ws && ws.readyState <= 1) ws.close();
          openSession().then(function() { receiveReplica(prompt); }).catch(function(err) {
            setGenerating(false); enableInput(); hideTokenCounter(); showError('Reconnection failed');
          });
          return;
        }
      }
      if (errMsg.indexOf('out of capacity') >= 0 || errMsg.indexOf('Too many concurrent') >= 0) {
        showError('Server is at capacity. Please try again later.');
      } else {
        showError('Generation error. Please try again.');
      }
      setGenerating(false); enableInput(); hideTokenCounter();
      return;
    }

    var token = response.outputs || '';
    if (token) {
      // Strip stop tokens and partial stop sequences from token
      token = token.split(config.chat.stop_token).join('');
      if (config.chat.extra_stop_sequences) {
        for (var i = 0; i < config.chat.extra_stop_sequences.length; i++) {
          token = token.split(config.chat.extra_stop_sequences[i]).join('');
        }
      }
      // Strip partial </s token and any leftover special tokens
      token = token.replace(/<\/s/g, '').replace(/<s/g, '').replace(/\[INST\]/g, '').replace(/\[\/INST\]/g, '');
      messages[messages.length - 1].content += token;
      // Clean accumulated text: remove trailing partial tags/tokens
      var cleanText = messages[messages.length - 1].content;
      cleanText = cleanText.replace(/<\/s>?$/g, '').replace(/<s>?$/g, '');
      cleanText = cleanText.replace(/\[\/?INST\]?$/g, '');
      cleanText = cleanText.trimEnd();
      messages[messages.length - 1].content = cleanText;
      updateLastAssistant(cleanText);

      // Check accumulated text for stop sequences
      var accumulated = messages[messages.length - 1].content;
      var stopIdx = checkStopSequences(accumulated);
      if (stopIdx >= 0) {
        messages[messages.length - 1].content = accumulated.substring(0, stopIdx).trimEnd();
        updateLastAssistant(messages[messages.length - 1].content);
        stopGenerating();
        return;
      }
    }

    // Update token counts
    tokenCount += response.token_count || 1;
    if (response.generated !== undefined) {
      generatedTokens = response.generated;
    } else {
      generatedTokens += response.token_count || 1;
    }
    updateTokenCounter();

    totalElapsed = (Date.now() - startTime) / 1000;
    if (totalElapsed > 0 && tokenCount > 1) {
      speedInfo.textContent = (tokenCount / totalElapsed).toFixed(1) + ' tok/s';
      speedInfo.classList.remove('hidden');
    }

    // HARD LIMIT: frontend force-stop if we've hit the token cap
    if (generatedTokens >= userMaxTokens) {
      // Trim to last complete sentence to avoid mid-word cutoff
      var trimmed = trimToLastSentence(messages[messages.length - 1].content);
      messages[messages.length - 1].content = trimmed;
      updateLastAssistant(trimmed);
      setGenerating(false);
      tokenCount = 0; totalElapsed = 0;
      hideTokenCounter();
      enableInput();
      position = 0;
      saveCurrentConversation();
      renderChatHistory();
      return;
    }

    if (response.stop || forceStop) {
      forceStop = false;
      setGenerating(false);
      tokenCount = 0; totalElapsed = 0;
      hideTokenCounter();
      enableInput();
      position = 0;
      saveCurrentConversation();
      renderChatHistory();
      return;
    }

    // Backend's inner loop generates all tokens autonomously.
    // Do NOT send follow-up generate requests here -- they pile up
    // in the WebSocket buffer and cause generation to restart in an
    // infinite loop after the token limit or stop sequence is hit.
  };

  ws.onerror = function() {
    setGenerating(false); enableInput(); setStatus('error'); hideTokenCounter();
    showError('Connection lost. Please try again.');
  };
}

// ==================== Settings ====================

function openSettings() {
  document.getElementById('settings-modal').classList.remove('hidden');
  document.getElementById('temp-slider').value = userTemperature;
  document.getElementById('temp-label').textContent = userTemperature;
  document.getElementById('topp-slider').value = userTopP;
  document.getElementById('topp-label').textContent = userTopP;
  document.getElementById('maxtokens-slider').value = userMaxTokens;
  document.getElementById('maxtokens-label').textContent = userMaxTokens;
}
function closeSettings() { document.getElementById('settings-modal').classList.add('hidden'); }
function updateTempLabel(v) { document.getElementById('temp-label').textContent = v; userTemperature = parseFloat(v); }
function updateTopPLabel(v) { document.getElementById('topp-label').textContent = v; userTopP = parseFloat(v); }
function updateMaxTokensLabel(v) { document.getElementById('maxtokens-label').textContent = v; userMaxTokens = parseInt(v); }
function resetSettings() {
  userTemperature = config.chat.generation_params.temperature || 0.6;
  userTopP = config.chat.generation_params.top_p || 0.9;
  userMaxTokens = config.chat.max_new_tokens || 500;
  document.getElementById('temp-slider').value = userTemperature;
  document.getElementById('temp-label').textContent = userTemperature;
  document.getElementById('topp-slider').value = userTopP;
  document.getElementById('topp-label').textContent = userTopP;
  document.getElementById('maxtokens-slider').value = userMaxTokens;
  document.getElementById('maxtokens-label').textContent = userMaxTokens;
}

// ==================== Network Status ====================

var networkStatusInterval = null;

function openNetworkStatus() {
  document.getElementById('network-status-modal').classList.remove('hidden');
  document.getElementById('ns-loading').classList.remove('hidden');
  document.getElementById('ns-content').classList.add('hidden');
  document.getElementById('ns-error').classList.add('hidden');
  fetchNetworkStatus();
  networkStatusInterval = setInterval(fetchNetworkStatus, 30000);
}

function closeNetworkStatus() {
  document.getElementById('network-status-modal').classList.add('hidden');
  if (networkStatusInterval) {
    clearInterval(networkStatusInterval);
    networkStatusInterval = null;
  }
}

function fetchNetworkStatus() {
  fetch('/api/status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      document.getElementById('ns-loading').classList.add('hidden');
      if (!data.ok) {
        document.getElementById('ns-content').classList.add('hidden');
        document.getElementById('ns-error').classList.remove('hidden');
        document.getElementById('ns-error-text').textContent = data.error || 'Failed to load status';
        return;
      }
      document.getElementById('ns-error').classList.add('hidden');
      document.getElementById('ns-content').classList.remove('hidden');
      renderNetworkStatus(data);
    })
    .catch(function(err) {
      document.getElementById('ns-loading').classList.add('hidden');
      document.getElementById('ns-content').classList.add('hidden');
      document.getElementById('ns-error').classList.remove('hidden');
      document.getElementById('ns-error-text').textContent = 'Network error: ' + err.message;
    });
}

function renderNetworkStatus(data) {
  document.getElementById('ns-model-name').textContent = data.model_name;
  document.getElementById('ns-peer-count').textContent = data.num_peers;
  document.getElementById('ns-coverage').textContent = data.block_coverage + ' / ' + data.total_blocks + ' blocks';
  document.getElementById('ns-speed').textContent = data.tokens_per_second > 0
    ? data.tokens_per_second + ' tok/s' : 'No data yet';
  document.getElementById('ns-uptime').textContent = formatUptime(data.uptime_seconds);
  document.getElementById('ns-block-end').textContent = 'Block ' + (data.total_blocks - 1);
  document.getElementById('ns-last-updated').textContent = formatTime(new Date());

  // Render block coverage bar
  var bar = document.getElementById('ns-block-bar');
  bar.innerHTML = '';
  for (var i = 0; i < data.block_status.length; i++) {
    var cell = document.createElement('div');
    cell.className = 'block-cell ' + (data.block_status[i] ? 'block-covered' : 'block-missing');
    cell.title = 'Block ' + i + (data.block_status[i] ? ' (covered)' : ' (missing)');
    bar.appendChild(cell);
  }

  // Render peer list
  var peerList = document.getElementById('ns-peer-list');
  peerList.innerHTML = '';
  if (data.peers.length === 0) {
    peerList.innerHTML = '<div class="text-xs text-gray-500 py-3 text-center">No peers online</div>';
    return;
  }
  for (var j = 0; j < data.peers.length; j++) {
    var p = data.peers[j];
    var item = document.createElement('div');
    item.className = 'peer-item';
    var endBlock = p.end - 1;
    item.innerHTML =
      '<span class="peer-id">...' + escapeHtml(p.peer_id) + '</span>' +
      '<span class="peer-blocks">Blocks ' + p.start + '-' + endBlock + ' (' + p.length + ')</span>' +
      '<span class="peer-throughput">' + p.throughput + ' tok/s</span>';
    peerList.appendChild(item);
  }
}

function formatUptime(seconds) {
  var d = Math.floor(seconds / 86400);
  var h = Math.floor((seconds % 86400) / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

// ==================== UI Helpers ====================

function showTyping() { typingIndicator.classList.remove('hidden'); scrollToBottom(); }
function hideTyping() { typingIndicator.classList.add('hidden'); }

function setGenerating(v) {
  generating = v;
  sendBtn.classList.toggle('hidden', v);
  stopBtn.classList.toggle('hidden', !v);
  if (v) { inputEl.disabled = true; }
  updateSendButton();
}

function enableInput() {
  inputEl.disabled = false;
  inputEl.focus();
  updateSendButton();
}

function updateSendButton() {
  sendBtn.disabled = !inputEl.value.trim() || generating;
}

function onInputChange(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 144) + 'px';
  var len = el.value.length;
  if (len > 0) {
    charCount.textContent = len;
    charCount.classList.remove('hidden');
  } else {
    charCount.classList.add('hidden');
  }
  updateSendButton();
}

function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function clearChat() { newChat(); }

inputEl.addEventListener('input', function() { updateSendButton(); });

// ==================== Init ====================

(function init() {
  var conv = { id: Date.now(), title: 'New Chat', messages: [] };
  conversations.push(conv);
  activeConvId = conv.id;
  renderChatHistory();

  inputEl.focus();
  openSession().catch(function(err) { showError('Failed to connect: ' + err); });
})();
