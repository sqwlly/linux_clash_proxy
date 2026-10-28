// cproxy Clash Verge (Rev) 全局扩展脚本 (Preprocess Script)
// 完整移植自 cproxy runtime.py：
// 1. 节点重名消歧、443 WebSocket CDN 独立 Host 优选、TCP Keep-Alive (15s) 与防断流参数加固
// 2. 自动构建 AI-GEMINI / AI-MANUAL / AI-AUTO / AI-US / AI-SG 策略组
// 3. 1X / WebSocket / gRPC 抗断流节点排在 IEPL / 裸 xtls-rprx-vision 之前，高倍率 (>=3X) 沉底
// 4. 剔除订阅冲突 AI 规则，置顶注入 Google/Gemini/Cloud Code 与 OpenAI/Codex/Claude 分流规则

const TEST_URL = "https://cp.cloudflare.com/generate_204";
const DEFAULT_MAX_AI_MULTIPLIER = 3.0;
const STAGGERED_AUTO_INTERVALS = [317, 323, 331, 337, 347, 349, 353, 359];
const AI_GEMINI_REGION_ORDER = ["KR", "TW", "GB", "DE", "FR", "BR", "AE", "AU"];
const AI_MANUAL_REGION_ORDER = ["US", "JP", "SG", "KR", "GB", "TW", "FR", "DE"];

const PANEL_INFO_RE = /剩余流量|套餐到期|距离下次重置|官网|群组|导航|下次重置|到期时间|流量重置|重置剩余|过期时间|有效期|Expire|Traffic|网址|公告|客服|频道|\b\d+(?:\.\d+)?\s*[KMGTPE]i?B\s*(?:[|/]|of)\s*\d+(?:\.\d+)?\s*[KMGTPE]i?B\b/i;

const REGION_PATTERNS = [
  ["HK", /香港|HK|Hong\s*Kong|🇭🇰/i],
  ["TW", /台湾|台北|TW|Taiwan|🇹🇼|🇨🇳/i],
  ["JP", /日本|东京|大阪|JP|Japan|🇯🇵/i],
  ["KR", /韩国|首尔|KR|Korea|🇰🇷/i],
  ["SG", /新加坡|狮城|SG|Singapore|🇸🇬/i],
  ["US", /美国|洛杉矶|圣何塞|硅谷|西雅图|芝加哥|达拉斯|纽约|US|United\s*States|America|🇺🇸/i],
  ["GB", /英国|伦敦|(?<!\d\s*)(?<!\d)\bGB\b|\bUK\b|United\s*Kingdom|Britain|🇬🇧/i],
  ["DE", /德国|法兰克福|DE|Germany|🇩🇪/i],
  ["FR", /法国|巴黎|FR|France|🇫🇷/i],
  ["BR", /巴西|圣保罗|BR|Brazil|🇧🇷/i],
  ["AE", /阿联酋|迪拜|AE|UAE|Dubai|🇦🇪/i],
  ["AU", /澳大利亚|澳洲|悉尼|AU|Australia|🇦🇺/i],
];

const AI_DOMAINS = new Set([
  "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
  "sora.com", "anthropic.com", "claude.ai", "claudeusercontent.com",
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

function matchRegion(name) {
  const text = String(name || "");
  for (const [code, regex] of REGION_PATTERNS) {
    if (regex.test(text)) return code;
  }
  return "OTHER";
}

function parseMultiplier(name) {
  const text = String(name || "");
  const m = text.match(/(?:×|x|X|\*)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:×|x|X|倍)/);
  if (!m) return 1.0;
  const val = parseFloat(m[1] || m[2]);
  return Number.isFinite(val) ? val : 1.0;
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
    hit = domain === "agy" || domain === "agy.exe" || domain.startsWith("^agy");
  } else {
    return false;
  }
  const target = parts[parts.length - 1];
  return hit && !["AI-MANUAL", "AI-GEMINI", "AI-AUTO"].includes(target);
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

  // 1. 消歧重名节点 & 优选 443 WS CDN 子域
  dedupeProxyNames(config);
  optimizeWsCdnServers(config.proxies);

  // 2. 清理易卡死 mihomo 的 geosite fallback-filter & 注入长连接保活设置
  if (config.dns && config.dns["fallback-filter"]) {
    delete config.dns["fallback-filter"].geosite;
  }
  if (!(config.dns && config.dns.ipv6 === true)) {
    config.ipv6 = false;
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
  const proxies = Array.isArray(config.proxies) ? config.proxies : [];
  const proxyMap = new Map();
  for (const p of proxies) {
    if (p && p.name) proxyMap.set(String(p.name), p);
  }

  const buckets = new Map();
  for (const code of ["US", "JP", "SG", "KR", "TW", "GB", "DE", "FR", "BR", "AE", "AU", "HK", "OTHER"]) {
    buckets.set(code, []);
  }
  for (const p of proxies) {
    if (!p || !p.name) continue;
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
    proxies.map(p => p && String(p.name || "")).filter(n => n && !isPanelInfoNode(n)),
    proxyMap
  );

  // 4. 构建 AI-GEMINI 候选池（韩/台/英/德/法/巴/阿/澳 优先，缺失时回退日/美/新）
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

  // 5. 构建 AI-US / AI-SG / AI-MANUAL 候选池
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

  const aiGroups = [
    { name: "AI-US", type: "fallback", proxies: usProxies, url: TEST_URL, interval: 283 },
    { name: "AI-SG", type: "fallback", proxies: sgProxies, url: TEST_URL, interval: 293 },
    { name: "AI-AUTO", type: "fallback", proxies: ["AI-US", "AI-SG"], url: TEST_URL, interval: 307 },
    { name: "AI-MANUAL", type: "fallback", proxies: manualCandidates, url: TEST_URL, interval: 300 },
    { name: "AI-GEMINI", type: "fallback", proxies: geminiProxies, url: TEST_URL, interval: 313 },
  ];

  const managedNames = new Set(["AI-US", "AI-SG", "AI-AUTO", "AI-MANUAL", "AI-GEMINI"]);
  const rawGroups = Array.isArray(config["proxy-groups"]) ? config["proxy-groups"] : [];
  const filteredGroups = rawGroups.filter(g => g && !managedNames.has(String(g.name || "")));

  // 清理自动测速组伪节点、升级 HTTPS 测速 URL 并错峰 300s 周期
  let staggerIdx = 0;
  for (const group of filteredGroups) {
    if (!group || typeof group !== "object") continue;
    const gtype = String(group.type || "").toLowerCase();
    if (Array.isArray(group.proxies)) {
      group.proxies = group.proxies.filter(m => !isPanelInfoNode(m));
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
  const aiRules = [
    "DOMAIN-SUFFIX,google.com,AI-GEMINI",
    "DOMAIN,play.googleapis.com,REJECT",
    "DOMAIN-SUFFIX,googleapis.com,AI-GEMINI",
    "DOMAIN-SUFFIX,googleusercontent.com,AI-GEMINI",
    "DOMAIN-SUFFIX,gstatic.com,AI-GEMINI",
    "DOMAIN-SUFFIX,google.dev,AI-GEMINI",
    "DOMAIN-SUFFIX,appspot.com,AI-GEMINI",
    "DOMAIN-KEYWORD,antigravity,AI-GEMINI",
    "DOMAIN-KEYWORD,gemini,AI-GEMINI",
    "DOMAIN-SUFFIX,goog,AI-GEMINI",
    "PROCESS-NAME,agy,AI-MANUAL",
    "PROCESS-NAME,agy.exe,AI-MANUAL",
    "PROCESS-NAME-REGEX,^agy\\.[0-9]+\\.old,AI-MANUAL",
    "DOMAIN-SUFFIX,openai.com,AI-MANUAL",
    "DOMAIN-SUFFIX,chatgpt.com,AI-MANUAL",
    "DOMAIN-SUFFIX,oaistatic.com,AI-MANUAL",
    "DOMAIN-SUFFIX,oaiusercontent.com,AI-MANUAL",
    "DOMAIN-SUFFIX,sora.com,AI-MANUAL",
    "DOMAIN,cdn.auth0.com,AI-MANUAL",
    "DOMAIN-SUFFIX,anthropic.com,AI-MANUAL",
    "DOMAIN-SUFFIX,claude.ai,AI-MANUAL",
    "DOMAIN-SUFFIX,claudeusercontent.com,AI-MANUAL",
    "DOMAIN-SUFFIX,x.ai,AI-MANUAL",
    "DOMAIN-SUFFIX,grok.com,AI-MANUAL",
    "DOMAIN-SUFFIX,meta.ai,AI-MANUAL",
    "DOMAIN-SUFFIX,llama.com,AI-MANUAL",
    "DOMAIN-SUFFIX,meta.com,AI-MANUAL",
    "DOMAIN-SUFFIX,openai.azure.com,AI-MANUAL",
    "DOMAIN-SUFFIX,githubcopilot.com,AI-MANUAL",
    "DOMAIN-SUFFIX,challenges.cloudflare.com,AI-MANUAL",
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
  ]);

  const frontSet = new Set([...aiRules, ...bulkDownloadDirect]);
  const rawRules = Array.isArray(config.rules) ? config.rules : [];
  const cleanRules = rawRules
    .map(normalizeIpCidrRule)
    .filter(r => !frontSet.has(r) && !staleRules.has(r) && !isAiConflictRule(r));

  config.rules = [...aiRules, ...bulkDownloadDirect, ...cleanRules];
  return config;
}
