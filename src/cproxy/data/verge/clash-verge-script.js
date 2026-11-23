// cproxy Clash Verge (Rev) 全局扩展脚本 (Preprocess Script)
// 完整移植自 cproxy runtime.py：
// 1. 节点重名消歧、443 WebSocket CDN 独立 Host 优选、TCP Keep-Alive (15s) 与防断流参数加固
// 2. 自动构建 AI-GEMINI / AI-CLAUDE / AI-MANUAL / AI-AUTO / AI-US / AI-SG 策略组
// 3. 1X / WebSocket / gRPC 抗断流节点排在 IEPL / 裸 xtls-rprx-vision 之前，高倍率 (>=3X) 沉底
// 4. 剔除订阅冲突 AI 规则，置顶注入 Google/Gemini/Cloud Code、Anthropic/Claude 与 OpenAI/Codex 分流规则

const TEST_URL = "https://cp.cloudflare.com/generate_204";
const GEMINI_TEST_URL = "https://www.gstatic.com/generate_204";
const KEEPALIVE_TEST_URL = "http://cp.cloudflare.com/generate_204";
const DEFAULT_KEEPALIVE_PORT = 7895;
const DEFAULT_MAX_AI_MULTIPLIER = 3.0;
const STAGGERED_AUTO_INTERVALS = [317, 323, 331, 337, 347, 349, 353, 359];
const AI_GEMINI_REGION_ORDER = ["TW", "KR", "GB", "DE", "FR", "BR", "AE", "AU"];
const AI_CLAUDE_REGION_ORDER = ["US", "JP", "SG", "GB", "DE", "FR", "TW", "KR"];
const AI_MANUAL_REGION_ORDER = ["US", "JP", "SG", "KR", "GB", "TW", "FR", "DE"];

const PANEL_INFO_RE = /剩余流量|套餐到期|距离下次重置|官网|群组|导航|下次重置|到期时间|流量重置|重置剩余|过期时间|有效期|Expire|Traffic|网址|公告|客服|频道|\b\d+(?:\.\d+)?\s*[KMGTPE]i?B\s*(?:[|/]|of)\s*\d+(?:\.\d+)?\s*[KMGTPE]i?B\b/i;

const REGION_PATTERNS = [
  ["HK", /🇭🇰|香港|Hong\s*Kong|(?<![A-Za-z0-9])HKG?(?![A-Za-z0-9])|(?<![A-Za-z0-9])HK\d/i],
  ["JP", /🇯🇵|日本|东京|東京|大阪|Japan|Tokyo|Osaka|(?<![A-Za-z0-9])JPN?(?![A-Za-z0-9])|(?<![A-Za-z0-9])JP\d/i],
  ["KR", /🇰🇷|韩国|韓國|首尔|首爾|釜山|Korea|Seoul|(?<![A-Za-z0-9])KOR?(?![A-Za-z0-9])|(?<![A-Za-z0-9])KR(?![A-Za-z0-9])|(?<![A-Za-z0-9])KR\d/i],
  ["SG", /🇸🇬|新加坡|狮城|獅城|Singapore|(?<![A-Za-z0-9])SGP?(?![A-Za-z0-9])|(?<![A-Za-z0-9])SG\d/i],
  ["TW", /🇹🇼|台湾|台灣|台北|新北|彰化|高雄|Taiwan|Taipei|Hinet|(?<![A-Za-z0-9])TWN?(?![A-Za-z0-9])|(?<![A-Za-z0-9])TW\d/i],
  ["US", /🇺🇸|美国|美國|洛杉矶|圣何塞|硅谷|西雅图|纽约|芝加哥|达拉斯|United\s*States|America|Los\s*Angeles|San\s*Jose|Silicon\s*Valley|Seattle|(?<![A-Za-z0-9])USA?(?![A-Za-z0-9])|(?<![A-Za-z0-9])US\d|(?<![A-Za-z0-9])(?:LAX|SJC|SEA)(?![A-Za-z0-9])/i],
  ["AU", /🇦🇺|澳大利亚|澳洲|悉尼|雪梨|墨尔本|Australia|Sydney|(?<![A-Za-z0-9])AUS?(?![A-Za-z0-9])|(?<![A-Za-z0-9])AU(?![A-Za-z0-9])|(?<![A-Za-z0-9])AU\d/i],
  ["BR", /🇧🇷|巴西|圣保罗|聖保羅|Brazil|Sao\s*Paulo|(?<![A-Za-z0-9])BRA?(?![A-Za-z0-9])|(?<![A-Za-z0-9])BR(?![A-Za-z0-9])|(?<![A-Za-z0-9])BR\d/i],
  ["AE", /🇦🇪|阿联酋|阿聯酋|迪拜|杜拜|Dubai|UAE|(?<![A-Za-z0-9])AE(?![A-Za-z0-9])/i],
  ["DE", /🇩🇪|德国|德國|法兰克福|法蘭克福|慕尼黑|Germany|Frankfurt|(?<![A-Za-z0-9])DEU?(?![A-Za-z0-9])|(?<![A-Za-z0-9])DE(?![A-Za-z0-9])|(?<![A-Za-z0-9])DE\d/i],
  ["FR", /🇫🇷|法国|法國|巴黎|France|Paris|(?<![A-Za-z0-9])FRA?(?![A-Za-z0-9])|(?<![A-Za-z0-9])FR(?![A-Za-z0-9])|(?<![A-Za-z0-9])FR\d/i],
  ["GB", /🇬🇧|英国|英國|伦敦|倫敦|United\s*Kingdom|Britain|London|England|(?<![A-Za-z0-9])UK(?![A-Za-z0-9])|(?<!\d\s*)(?<!\d)(?<![A-Za-z0-9])GBR?(?![A-Za-z0-9])|(?<![A-Za-z0-9])GB\d/i],
];

const AI_DOMAINS = new Set([
  "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
  "chatgpt.livekit.cloud", "host.livekit.cloud", "turn.livekit.cloud",
  "openai-api.arkoselabs.com", "client-api.arkoselabs.com",
  "featuregates.org", "api.statsig.com", "events.statsigapi.net",
  "openaiapi-site.azureedge.net", "openaicom.imgix.net",
  "openaicomproductionae4b.blob.core.windows.net",
  "production-openaicom-storage.azureedge.net",
  "openaicom-api-bdcpf8c6d2e9atf6.z01.azurefd.net",
  "chat.openai.com.cdn.cloudflare.net",
  "sora.com", "anthropic.com", "claude.ai", "claude.com", "claudeusercontent.com",
  "x.ai", "grok.com", "openai.azure.com", "githubcopilot.com",
  "meta.ai", "llama.com", "meta.com",
  "challenges.cloudflare.com",
  "google.com", "googleapis.com", "googleusercontent.com", "gstatic.com",
  "google.dev", "appspot.com", "goog", "gemini.google.com", "aistudio.google.com",
  "ai.google.dev", "generativelanguage.googleapis.com", "cdn.auth0.com",
]);

const AI_CONFLICT_KEYWORDS = new Set([
  "openai", "chatgpt", "oaistatic", "oaiusercontent", "anthropic",
  "claude", "claudeusercontent", "gemini", "copilot", "grok",
  "google", "antigravity",
]);

function isPanelInfoNode(name) {
  return !name || PANEL_INFO_RE.test(String(name));
}

function isLocalLoopbackProxy(proxy) {
  if (!proxy || typeof proxy !== "object") return false;
  const srv = String(proxy.server || "").trim().toLowerCase();
  return srv === "127.0.0.1" || srv === "localhost" || srv === "::1";
}

function matchRegion(name) {
  const text = String(name || "");
  for (const [code, regex] of REGION_PATTERNS) {
    if (regex.test(text)) return code;
  }
  return "OTHER";
}

function parseMultiplier(name) {
  const text = String(name || "");
  const patterns = [
    /(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[xX×](?![A-Za-z0-9])/,
    /(?<![A-Za-z0-9])[xX×]\s*(\d+(?:\.\d+)?)(?![A-Za-z0-9])/,
    /(\d+(?:\.\d+)?)\s*倍(?:率)?/,
  ];
  for (const re of patterns) {
    const m = text.match(re);
    if (m) {
      const val = parseFloat(m[1]);
      if (Number.isFinite(val)) return val;
    }
  }
  return 1.0;
}

function isStreamFragileProxy(proxy, nameFallback = "") {
  const nodeName = String(nameFallback || (proxy && proxy.name) || "").toUpperCase();
  if (nodeName.includes("IEPL") || nodeName.includes("IPLC") || nodeName.includes("内网专线")) {
    return true;
  }
  if (!proxy || typeof proxy !== "object") return false;
  const flow = String(proxy.flow || "").trim().toLowerCase();
  const network = String(proxy.network || "tcp").trim().toLowerCase();
  return flow === "xtls-rprx-vision" && network !== "ws" && network !== "grpc";
}

function preferNonIeplNodes(names, proxyMap) {
  const clean = [];
  const fragile = [];
  for (const name of names) {
    if (!name) continue;
    const proxy = proxyMap ? proxyMap.get(name) : null;
    if (isStreamFragileProxy(proxy, name)) {
      fragile.push(name);
    } else {
      clean.push(name);
    }
  }
  return [...clean, ...fragile];
}

function deprioritizeHighMultiplier(names, maxMult = DEFAULT_MAX_AI_MULTIPLIER) {
  if (maxMult <= 0) return [...names];
  const normal = [];
  const high = [];
  for (const name of names) {
    if (parseMultiplier(name) < maxMult) {
      normal.push(name);
    } else {
      high.push(name);
    }
  }
  return [...normal, ...high];
}

function dedupeProxyNames(config) {
  const proxies = config.proxies;
  if (!Array.isArray(proxies)) return;
  const reserved = new Set(proxies.map(p => p && String(p.name || "")).filter(Boolean));
  const seen = new Map();
  const suffixMap = new Map();

  for (const proxy of proxies) {
    if (!proxy || typeof proxy !== "object" || !proxy.name) continue;
    const name = String(proxy.name);
    const count = (seen.get(name) || 0) + 1;
    seen.set(name, count);
    if (count === 1) continue;
    let suffix = count;
    let newName = `${name} #${suffix}`;
    while (reserved.has(newName)) {
      suffix += 1;
      newName = `${name} #${suffix}`;
    }
    reserved.add(newName);
    proxy.name = newName;
    if (!suffixMap.has(name)) suffixMap.set(name, []);
    suffixMap.get(name).push(newName);
  }

  if (suffixMap.size === 0 || !Array.isArray(config["proxy-groups"])) return;
  for (const group of config["proxy-groups"]) {
    if (!group || !Array.isArray(group.proxies)) continue;
    const groupSeen = new Map();
    group.proxies = group.proxies.map(member => {
      const m = String(member);
      if (!suffixMap.has(m)) return member;
      const idx = groupSeen.get(m) || 0;
      groupSeen.set(m, idx + 1);
      if (idx === 0) return member;
      const replacements = suffixMap.get(m);
      return replacements[idx - 1] || member;
    });
  }
}

function optimizeWsCdnServers(proxies) {
  if (!Array.isArray(proxies)) return;
  for (const proxy of proxies) {
    if (!proxy || typeof proxy !== "object") continue;
    if (String(proxy.network || "").trim().toLowerCase() !== "ws") continue;
    if (Number(proxy.port) !== 443) continue;
    const server = String(proxy.server || "").trim();
    const sni = String(proxy.servername || proxy.sni || "").trim().toLowerCase();
    const headers = proxy["ws-opts"] && proxy["ws-opts"].headers;
    const wsHost = headers ? String(headers.Host || headers.host || "").trim() : "";
    if (!server || !sni || !wsHost) continue;
    if (wsHost.toLowerCase() !== server.toLowerCase() && wsHost.toLowerCase().endsWith("." + sni)) {
      proxy.server = wsHost;
    }
  }
}

function tuneAnytlsProxies(proxies) {
  if (!Array.isArray(proxies)) return;
  for (const proxy of proxies) {
    if (!proxy || typeof proxy !== "object") continue;
    if (String(proxy.type || "").trim().toLowerCase() !== "anytls") continue;
    const rawTimeout = proxy["idle-session-timeout"];
    if (typeof rawTimeout === "number" && rawTimeout > 0 && rawTimeout < 120) {
      proxy["idle-session-timeout"] = 120;
    }
    const rawCheck = proxy["idle-session-check-interval"];
    if (typeof rawCheck === "number" && rawCheck > 0 && rawCheck < 60) {
      proxy["idle-session-check-interval"] = 60;
    }
  }
}

function isAiConflictRule(rule) {
  if (typeof rule !== "string") return false;
  const parts = rule.split(",").map(s => s.trim());
  if (parts.length < 3) return false;
  const rtype = parts[0].toUpperCase();
  const domain = parts[1].toLowerCase();
  let hit = false;
  if (rtype === "DOMAIN" || rtype === "DOMAIN-SUFFIX") {
    hit = AI_DOMAINS.has(domain);
  } else if (rtype === "DOMAIN-KEYWORD") {
    hit = AI_CONFLICT_KEYWORDS.has(domain);
  } else if (rtype === "PROCESS-NAME" || rtype === "PROCESS-NAME-REGEX") {
    hit = ["agy", "agy.exe", "codex", "codex.exe", "chatgpt.exe"].includes(domain) || domain.startsWith("^agy");
  } else {
    return false;
  }
  const target = parts[parts.length - 1];
  return hit && ![
    "AI-MANUAL",
    "AI-GEMINI",
    "AI-CLAUDE",
    "AI-AUTO",
    "AI-MANUAL-KEEPALIVE",
    "AI-GEMINI-KEEPALIVE",
    "AI-CLAUDE-KEEPALIVE",
    "H2-KEEPALIVE",
  ].includes(target);
}

function normalizeIpCidrRule(rule) {
  if (typeof rule !== "string") return rule;
  const parts = rule.split(",").map(s => s.trim());
  if (parts.length === 3 && (parts[0].toUpperCase() === "IP-CIDR" || parts[0].toUpperCase() === "IP-CIDR6")) {
    return `${parts[0]},${parts[1]},${parts[2]},no-resolve`;
  }
  return rule;
}

function main(config) {
  if (!config || typeof config !== "object") return config;

  // 1. 消歧重名节点、放宽 anytls 空闲会话超时 & 优选 443 WS CDN 子域
  dedupeProxyNames(config);
  tuneAnytlsProxies(config.proxies);
  optimizeWsCdnServers(config.proxies);

  // 2. 清理易卡死 mihomo 的 geosite fallback-filter、补齐游戏/加速器 fake-ip-filter & 注入长连接保活设置
  if (config.dns && config.dns["fallback-filter"]) {
    delete config.dns["fallback-filter"].geosite;
  }
  if (config.dns && typeof config.dns === "object") {
    const extraFakeIpFilter = [
      "+.leigod.com",
      "+.leigodtech.com",
      "+.nn.com",
      "+.playbattlegrounds.com",
      "+.playbattlegrounds.com.cn",
      "+.krafton.com",
      "+.krafton.io",
      "+.pubg.com",
      "+.battleye.com",
      "+.eac-cdn.com",
      "+.easyanticheat.net",
      "+.steamserver.net",
      "+.kookapp.cn",
      "+.kaiheila.cn",
      "stun.*.*",
      "stun.*.*.*",
    ];
    const curFilter = Array.isArray(config.dns["fake-ip-filter"]) ? [...config.dns["fake-ip-filter"]] : [];
    const filterSet = new Set(curFilter);
    for (const item of extraFakeIpFilter) {
      if (!filterSet.has(item)) {
        curFilter.push(item);
        filterSet.add(item);
      }
    }
    config.dns["fake-ip-filter"] = curFilter;
  }
  if (!(config.dns && config.dns.ipv6 === true)) {
    config.ipv6 = false;
  }
  if (config.tun && typeof config.tun === "object") {
    if (!config.tun.stack || config.tun.stack === "gvisor") {
      config.tun.stack = "system";
    }
  }
  config["tcp-concurrent"] = false;
  config["unified-delay"] = true;
  if (!config["find-process-mode"]) {
    config["find-process-mode"] = "always";
  }
  config["disable-keep-alive"] = false;
  if (!config["keep-alive-idle"]) config["keep-alive-idle"] = 15;
  if (!config["keep-alive-interval"]) config["keep-alive-interval"] = 15;

  // 3. 按地区分桶并执行非 IEPL/非裸 Vision 优先排序
  const rawProxiesList = Array.isArray(config.proxies)
    ? config.proxies.filter(p => !(p && typeof p === "object" && p.name === "H2-KEEPALIVE"))
    : [];
  const proxies = rawProxiesList;
  const proxyMap = new Map();
  for (const p of proxies) {
    if (p && p.name) proxyMap.set(String(p.name), p);
  }

  const buckets = new Map();
  for (const code of ["US", "JP", "SG", "KR", "TW", "GB", "DE", "FR", "BR", "AE", "AU", "HK", "OTHER"]) {
    buckets.set(code, []);
  }
  for (const p of proxies) {
    if (!p || !p.name || isLocalLoopbackProxy(p)) continue;
    const name = String(p.name);
    if (isPanelInfoNode(name)) continue;
    const code = matchRegion(name);
    if (!buckets.has(code)) buckets.set(code, []);
    buckets.get(code).push(name);
  }
  for (const [code, list] of buckets.entries()) {
    buckets.set(code, preferNonIeplNodes(list, proxyMap));
  }

  const allValidNodes = preferNonIeplNodes(
    proxies
      .filter(p => p && p.name && !isLocalLoopbackProxy(p))
      .map(p => String(p.name))
      .filter(n => n && !isPanelInfoNode(n)),
    proxyMap
  );
  if (allValidNodes.length === 0) {
    return config;
  }

  // 4. 构建 AI-GEMINI 候选池（台/韩/英/德/法/巴/阿/澳 优先，缺失时回退日/美/新）
  let geminiLeaves = [];
  for (const code of AI_GEMINI_REGION_ORDER) {
    for (const n of buckets.get(code) || []) {
      if (!geminiLeaves.includes(n)) geminiLeaves.push(n);
    }
  }
  geminiLeaves = preferNonIeplNodes(geminiLeaves, proxyMap);
  if (geminiLeaves.length === 0) {
    for (const code of ["JP", "US", "SG"]) {
      for (const n of buckets.get(code) || []) {
        if (!geminiLeaves.includes(n)) geminiLeaves.push(n);
      }
    }
  }
  if (geminiLeaves.length === 0) {
    geminiLeaves = [...allValidNodes];
  }
  const geminiProxies = deprioritizeHighMultiplier(geminiLeaves);

  // 5. 构建 AI-CLAUDE / AI-US / AI-SG / AI-MANUAL 候选池
  let claudeLeaves = [];
  for (const code of AI_CLAUDE_REGION_ORDER) {
    for (const n of buckets.get(code) || []) {
      if (!claudeLeaves.includes(n)) claudeLeaves.push(n);
    }
  }
  if (claudeLeaves.length === 0) {
    claudeLeaves = [...allValidNodes];
  }
  const claudeProxies = deprioritizeHighMultiplier(preferNonIeplNodes(claudeLeaves, proxyMap));

  let usProxies = deprioritizeHighMultiplier(buckets.get("US") || []);
  let sgProxies = deprioritizeHighMultiplier(buckets.get("SG") || []);
  const fallbackPool = usProxies.length
    ? usProxies
    : sgProxies.length
    ? sgProxies
    : geminiProxies.length
    ? geminiProxies
    : allValidNodes;
  if (usProxies.length === 0) usProxies = [...fallbackPool];
  if (sgProxies.length === 0) sgProxies = [...fallbackPool];

  let manualLeaves = [];
  for (const code of AI_MANUAL_REGION_ORDER) {
    for (const n of buckets.get(code) || []) {
      if (!manualLeaves.includes(n)) manualLeaves.push(n);
    }
  }
  if (manualLeaves.length === 0) {
    manualLeaves = [...allValidNodes];
  }
  manualLeaves = deprioritizeHighMultiplier(preferNonIeplNodes(manualLeaves, proxyMap));
  const manualCandidates = [...manualLeaves, "AI-AUTO"];

  config.proxies = [
    ...rawProxiesList,
    {
      name: "H2-KEEPALIVE",
      type: "http",
      server: "127.0.0.1",
      port: DEFAULT_KEEPALIVE_PORT,
    },
  ];

  const aiGroups = [
    { name: "AI-US", type: "fallback", proxies: usProxies, url: TEST_URL, interval: 283 },
    { name: "AI-SG", type: "fallback", proxies: sgProxies, url: TEST_URL, interval: 293 },
    { name: "AI-AUTO", type: "fallback", proxies: ["AI-US", "AI-SG"], url: TEST_URL, interval: 307 },
    { name: "AI-MANUAL", type: "fallback", proxies: manualCandidates, url: TEST_URL, interval: 300 },
    {
      name: "AI-GEMINI",
      type: "fallback",
      proxies: geminiProxies,
      url: GEMINI_TEST_URL,
      interval: 60,
      lazy: false,
      "max-failed-times": 1,
      "expected-status": 204,
    },
    {
      name: "AI-CLAUDE",
      type: "fallback",
      proxies: claudeProxies,
      url: TEST_URL,
      interval: 120,
      lazy: false,
      "max-failed-times": 2,
      "expected-status": 204,
    },
    {
      name: "AI-MANUAL-KEEPALIVE",
      type: "fallback",
      proxies: ["H2-KEEPALIVE", "AI-MANUAL"],
      url: KEEPALIVE_TEST_URL,
      interval: 15,
      lazy: false,
    },
    {
      name: "AI-GEMINI-KEEPALIVE",
      type: "fallback",
      proxies: ["H2-KEEPALIVE", "AI-GEMINI"],
      url: KEEPALIVE_TEST_URL,
      interval: 15,
      lazy: false,
    },
    {
      name: "AI-CLAUDE-KEEPALIVE",
      type: "fallback",
      proxies: ["H2-KEEPALIVE", "AI-CLAUDE"],
      url: KEEPALIVE_TEST_URL,
      interval: 15,
      lazy: false,
    },
  ];

  const managedNames = new Set([
    "AI-US",
    "AI-SG",
    "AI-AUTO",
    "AI-MANUAL",
    "AI-GEMINI",
    "AI-CLAUDE",
    "AI-MANUAL-KEEPALIVE",
    "AI-GEMINI-KEEPALIVE",
    "AI-CLAUDE-KEEPALIVE",
  ]);
  const rawGroups = Array.isArray(config["proxy-groups"]) ? config["proxy-groups"] : [];
  const filteredGroups = rawGroups.filter(g => g && !managedNames.has(String(g.name || "")));

  // 清理自动测速组伪节点、升级 HTTPS 测速 URL 并错峰 300s 周期
  let staggerIdx = 0;
  for (const group of filteredGroups) {
    if (!group || typeof group !== "object") continue;
    const gtype = String(group.type || "").toLowerCase();
    if (Array.isArray(group.proxies)) {
      const cleaned = group.proxies.filter(m => !isPanelInfoNode(m));
      if (cleaned.length > 0) {
        group.proxies = cleaned;
      }
    }
    if (["url-test", "urltest", "fallback", "load-balance", "loadbalance"].includes(gtype)) {
      if (typeof group.url === "string" && group.url.trim().toLowerCase().startsWith("http://")) {
        group.url = TEST_URL;
      }
      if (group.interval === 300) {
        group.interval = STAGGERED_AUTO_INTERVALS[staggerIdx % STAGGERED_AUTO_INTERVALS.length];
        staggerIdx += 1;
      }
    }
  }

  config["proxy-groups"] = [...aiGroups, ...filteredGroups];

  // 6. 置顶注入 AI 与大流量下载直连规则，剔除订阅冲突规则
  const geminiKeepaliveRule =
    "AND,((OR,((DOMAIN,daily-cloudcode-pa.googleapis.com),(DOMAIN,aicode.googleapis.com))),(NETWORK,TCP),(OR,((IN-TYPE,HTTP),(IN-TYPE,TUN)))),AI-GEMINI-KEEPALIVE";
  const codexKeepaliveRule =
    "AND,((OR,((DOMAIN,chatgpt.com),(DOMAIN,ws.chatgpt.com),(DOMAIN,api.openai.com))),(NETWORK,TCP),(OR,((IN-TYPE,HTTP),(IN-TYPE,TUN))),(NOT,((OR,((PROCESS-NAME,chrome.exe),(PROCESS-NAME,msedge.exe),(PROCESS-NAME,firefox.exe),(PROCESS-NAME,brave.exe)))))),AI-MANUAL-KEEPALIVE";
  const claudeKeepaliveRule =
    "AND,((DOMAIN,api.anthropic.com),(NETWORK,TCP),(OR,((IN-TYPE,HTTP),(IN-TYPE,TUN))),(NOT,((OR,((PROCESS-NAME,chrome.exe),(PROCESS-NAME,msedge.exe),(PROCESS-NAME,firefox.exe),(PROCESS-NAME,brave.exe)))))),AI-CLAUDE-KEEPALIVE";

  const aiRules = [
    "DOMAIN-SUFFIX,google.com,AI-GEMINI",
    "DOMAIN,play.googleapis.com,REJECT",
    geminiKeepaliveRule,
    "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI",
    "DOMAIN-SUFFIX,googleusercontent.com,AI-GEMINI",
    "DOMAIN-SUFFIX,gstatic.com,AI-GEMINI",
    "DOMAIN-SUFFIX,google.dev,AI-GEMINI",
    "DOMAIN-SUFFIX,appspot.com,AI-GEMINI",
    "DOMAIN-KEYWORD,antigravity,AI-GEMINI",
    "DOMAIN-KEYWORD,gemini,AI-GEMINI",
    "DOMAIN-SUFFIX,goog,AI-GEMINI",
    codexKeepaliveRule,
    "PROCESS-NAME,agy,AI-MANUAL",
    "PROCESS-NAME,agy.exe,AI-MANUAL",
    "PROCESS-NAME-REGEX,^agy\\.[0-9]+\\.old,AI-MANUAL",
    "PROCESS-NAME-REGEX,^agy\\.exe\\.[0-9]+\\.old,AI-MANUAL",
    "PROCESS-NAME,codex,AI-MANUAL",
    "PROCESS-NAME,codex.exe,AI-MANUAL",
    "PROCESS-NAME,ChatGPT.exe,AI-MANUAL",
    "DOMAIN-SUFFIX,openai.com,AI-MANUAL",
    "DOMAIN-SUFFIX,chatgpt.com,AI-MANUAL",
    "DOMAIN-SUFFIX,oaistatic.com,AI-MANUAL",
    "DOMAIN-SUFFIX,oaiusercontent.com,AI-MANUAL",
    "DOMAIN-SUFFIX,chatgpt.livekit.cloud,AI-MANUAL",
    "DOMAIN-SUFFIX,host.livekit.cloud,AI-MANUAL",
    "DOMAIN-SUFFIX,turn.livekit.cloud,AI-MANUAL",
    "DOMAIN,openai-api.arkoselabs.com,AI-MANUAL",
    "DOMAIN-SUFFIX,client-api.arkoselabs.com,AI-MANUAL",
    "DOMAIN-SUFFIX,featuregates.org,AI-MANUAL",
    "DOMAIN-SUFFIX,api.statsig.com,AI-MANUAL",
    "DOMAIN-SUFFIX,events.statsigapi.net,AI-MANUAL",
    "DOMAIN-SUFFIX,openaiapi-site.azureedge.net,AI-MANUAL",
    "DOMAIN-SUFFIX,openaicom.imgix.net,AI-MANUAL",
    "DOMAIN,openaicomproductionae4b.blob.core.windows.net,AI-MANUAL",
    "DOMAIN,production-openaicom-storage.azureedge.net,AI-MANUAL",
    "DOMAIN,openaicom-api-bdcpf8c6d2e9atf6.z01.azurefd.net,AI-MANUAL",
    "DOMAIN,chat.openai.com.cdn.cloudflare.net,AI-MANUAL",
    "DOMAIN-SUFFIX,sora.com,AI-MANUAL",
    "DOMAIN,cdn.auth0.com,AI-MANUAL",
    claudeKeepaliveRule,
    "DOMAIN-SUFFIX,anthropic.com,AI-CLAUDE",
    "DOMAIN-SUFFIX,claude.ai,AI-CLAUDE",
    "DOMAIN-SUFFIX,claude.com,AI-CLAUDE",
    "DOMAIN-SUFFIX,claudeusercontent.com,AI-CLAUDE",
    "DOMAIN-SUFFIX,x.ai,AI-MANUAL",
    "DOMAIN-SUFFIX,grok.com,AI-MANUAL",
    "DOMAIN-SUFFIX,meta.ai,AI-MANUAL",
    "DOMAIN-SUFFIX,llama.com,AI-MANUAL",
    "DOMAIN-SUFFIX,meta.com,AI-MANUAL",
    "DOMAIN-SUFFIX,openai.azure.com,AI-MANUAL",
    "DOMAIN-SUFFIX,githubcopilot.com,AI-MANUAL",
    "DOMAIN-SUFFIX,challenges.cloudflare.com,AI-MANUAL",
  ];

  const gameDirectRules = [
    // Steam 与语音/游戏辅助
    "PROCESS-NAME,steam.exe,DIRECT",
    "PROCESS-NAME,steamservice.exe,DIRECT",
    "PROCESS-NAME,steamwebhelper.exe,DIRECT",
    "PROCESS-NAME,kook.exe,DIRECT",
    "PROCESS-NAME,GamePP.exe,DIRECT",
    // 雷神加速器（Electron 壳 + 真核心隧道引擎 leishenSdk + 本地 SSL 反代）
    "PROCESS-NAME,leigod.exe,DIRECT",
    "PROCESS-NAME,leigod_launcher.exe,DIRECT",
    "PROCESS-NAME,leishenSdk.exe,DIRECT",
    "PROCESS-NAME,networktunnel_proxy.exe,DIRECT",
    "PROCESS-NAME,xroute.exe,DIRECT",
    "PROCESS-NAME,leigod_pubg_tool.exe,DIRECT",
    "PROCESS-NAME,leigod-tool.exe,DIRECT",
    // UU 加速器
    "PROCESS-NAME,uu.exe,DIRECT",
    "PROCESS-NAME,uu_ball.exe,DIRECT",
    "PROCESS-NAME,uu_cloudsyn.exe,DIRECT",
    "PROCESS-NAME,uu_launcher.exe,DIRECT",
    // PUBG 绝地求生及反作弊（BattlEye / Zakynthos / 内置 CEF 大厅）
    "PROCESS-NAME,TslGame.exe,DIRECT",
    "PROCESS-NAME,TslGame_BE.exe,DIRECT",
    "PROCESS-NAME,TslGame_ZK.exe,DIRECT",
    "PROCESS-NAME,TslGame_UC.exe,DIRECT",
    "PROCESS-NAME,ExecPubg.exe,DIRECT",
    "PROCESS-NAME,BEService.exe,DIRECT",
    "PROCESS-NAME,BEService_x64.exe,DIRECT",
    "PROCESS-NAME,zksvc.exe,DIRECT",
    "PROCESS-NAME,BroCrashReporter.exe,DIRECT",
    "PROCESS-NAME-REGEX,^UnrealCEFSubProcess.*\\.exe$,DIRECT",
    // 通用反作弊与常见联机游戏
    "PROCESS-NAME,EasyAntiCheat.exe,DIRECT",
    "PROCESS-NAME,EasyAntiCheat_EOS.exe,DIRECT",
    "PROCESS-NAME,cs2.exe,DIRECT",
    "PROCESS-NAME,dota2.exe,DIRECT",
    "PROCESS-NAME,RustClient.exe,DIRECT",
    "PROCESS-NAME,Rust.exe,DIRECT",
    "PROCESS-NAME,RidersRepublic.exe,DIRECT",
    "PROCESS-NAME,RidersRepublic_BE.exe,DIRECT",
    "PROCESS-NAME,RDR2.exe,DIRECT",
    "PROCESS-NAME,PlayRDR2.exe,DIRECT",
    "PROCESS-NAME,AimLab_tb.exe,DIRECT",
    "PROCESS-NAME,b1-Win64-Shipping.exe,DIRECT",
    // 加速器与语音控制面域名直连
    "DOMAIN-SUFFIX,kookapp.cn,DIRECT",
    "DOMAIN-SUFFIX,kaiheila.cn,DIRECT",
    "DOMAIN-SUFFIX,leigod.com,DIRECT",
    "DOMAIN-SUFFIX,leigodtech.com,DIRECT",
    "DOMAIN-SUFFIX,nn.com,DIRECT",
  ];

  const bulkDownloadDirect = [
    "DOMAIN-SUFFIX,pytorch.org,DIRECT",
    "DOMAIN-SUFFIX,pypi.org,DIRECT",
    "DOMAIN-SUFFIX,pythonhosted.org,DIRECT",
    "DOMAIN-SUFFIX,npmjs.org,DIRECT",
    "DOMAIN-SUFFIX,npmmirror.com,DIRECT",
    "DOMAIN-SUFFIX,moonshot.cn,DIRECT",
    "DOMAIN-SUFFIX,minimaxi.com,DIRECT",
    "DOMAIN-SUFFIX,minimax.io,DIRECT",
    "DOMAIN-SUFFIX,deepseek.com,DIRECT",
    "DOMAIN-SUFFIX,dashscope.aliyuncs.com,DIRECT",
  ];

  const staleRules = new Set([
    "DOMAIN-SUFFIX,cursor.sh,DIRECT",
    "DOMAIN,safebrowsing.googleapis.com,DIRECT",
    "GEOIP,CN,DIRECT",
    "AND,((DOMAIN,daily-cloudcode-pa.googleapis.com),(IN-TYPE,HTTP)),H2-KEEPALIVE",
    "AND,((DOMAIN,daily-cloudcode-pa.googleapis.com),(NOT,((IN-TYPE,SOCKS5)))),H2-KEEPALIVE",
    "AND,((OR,((DOMAIN,chatgpt.com),(DOMAIN,ws.chatgpt.com))),(NETWORK,TCP),(OR,((IN-TYPE,HTTP),(IN-TYPE,TUN)))),H2-KEEPALIVE",
    "DOMAIN-SUFFIX,anthropic.com,AI-MANUAL",
    "DOMAIN-SUFFIX,claude.ai,AI-MANUAL",
    "DOMAIN-SUFFIX,claude.com,AI-MANUAL",
    "DOMAIN-SUFFIX,claudeusercontent.com,AI-MANUAL",
  ]);
  const isLegacyKeepaliveRule = rule => {
    if (typeof rule !== "string") return false;
    const parts = rule.split(",").map(s => s.trim());
    if (parts.length < 2) return false;
    const target = parts[parts.length - 1];
    return (
      target === "H2-KEEPALIVE" ||
      target === "AI-MANUAL-KEEPALIVE" ||
      target === "AI-GEMINI-KEEPALIVE" ||
      target === "AI-CLAUDE-KEEPALIVE"
    );
  };

  const frontSet = new Set([...aiRules, ...gameDirectRules, ...bulkDownloadDirect]);
  const rawRules = Array.isArray(config.rules) ? config.rules : [];
  const cleanRules = rawRules
    .map(normalizeIpCidrRule)
    .filter(r => !frontSet.has(r) && !staleRules.has(r) && !isLegacyKeepaliveRule(r) && !isAiConflictRule(r));

  config.rules = [...aiRules, ...gameDirectRules, ...bulkDownloadDirect, ...cleanRules];
  return config;
}
