import { sampleTokens } from "./reference-data.js";

// Numeric capacities are editorial estimates. String keys select a calibrated text.
// These denominators are used only for the display caption.
function ref(unit, name, capacity) {
  const tokens = typeof capacity === "number" ? capacity : sampleTokens[capacity];
  return {unit, name, tokens};
}

const quantity = new Intl.NumberFormat("zh-CN", {maximumFractionDigits:1});
const largeQuantity = new Intl.NumberFormat("zh-CN", {notation:"compact", maximumFractionDigits:1});
const bands = [
  {floor:1, items:[
    ref("条", "只写「好」的回复", "reply"),
    ref("个", "中文句号", "dot"),
    ref("个", "中文问号", "question"),
  ]},
  {floor:30, items:[
    ref("条", "《黑暗之魂》的橙色建言", 15),
    ref("张", "「青眼白龙」的卡面描述", 45),
    ref("条", "初代《宝可梦》的图鉴词条", 55),
    ref("遍", "《银翼杀手》雨中的临终独白", 60),
    ref("张", "长到小腿肚的便利店小票", 120),
  ]},
  {floor:500, items:[
    ref("遍", "Pink Floyd《Wish You Were Here》的歌词", 240),
    ref("遍", "草东没有派对《山海》的歌词", 280),
    ref("遍", "Queen《Bohemian Rhapsody》的歌词", 480),
    ref("篇", "诸葛亮的《出师表》", 850),
    ref("集", "《虫师》的对白", 2200),
  ]},
  {floor:5_000, items:[
    ref("集", "《星际牛仔》的对白", 3500),
    ref("张", "Pink Floyd《The Wall》的专辑歌词本", 6000),
    ref("部", "《道德经》", "tao"),
    ref("部", "《重庆森林》的对白剧本", 14000),
    ref("本", "《小王子》英译本", 22000),
    ref("套", "Radiohead 九张录音室专辑的歌词", 22500),
  ]},
  {floor:50_000, items:[
    ref("套", "《血源诅咒》的物品说明与 NPC 对白", 55000),
    ref("遍", "TV 版《新世纪福音战士》全 26 集台词", 75000),
    ref("套", "《空洞骑士》的碑文、梦境文字与对白", 80000),
    ref("本", "《1984》英文原著", 120000),
    ref("本", "《黑神话：悟空》的《影神图》", 140000),
    ref("部", "《三体》第一部", 250000),
  ]},
  {floor:500_000, items:[
    ref("本", "《罪与罚》英译本", "crime"),
    ref("套", "《魔戒》英文三部曲", 630000),
    ref("部", "《红楼梦》", "red"),
    ref("套", "《哈利·波特》英文七部曲", 1450000),
    ref("份", "《极乐迪斯科》的对白与脑内独白", 1500000),
    ref("份", "《博德之门 3》的游戏对白剧本", 2600000),
  ]},
  {floor:5_000_000, items:[
    ref("部", "《全唐诗》", 4200000),
    ref("套", "《海贼王》漫画的对白", 4500000),
    ref("套", "《鲁迅全集》", 8500000),
    ref("套", "《最终幻想 14》从重生之境到黄金的遗产的主线剧本", 35000000),
    ref("套", "《二十四史》", 50000000),
    ref("套", "《大英百科全书》32 卷本", 58000000),
  ]},
  {floor:80_000_000, items:[
    ref("份", "Linux 内核源码", 380000000),
    ref("套", "文渊阁《四库全书》", 1000000000),
    ref("套", "《二十四史》", 50000000),
  ]},
  {floor:2_000_000_000, items:[
    ref("份", "英文维基百科的条目正文", 6000000000),
    ref("套", "文渊阁《四库全书》", 1000000000),
    ref("份", "Linux 内核源码", 380000000),
  ]},
];

export function tokenBand(total) {
  if (!Number.isFinite(total) || total < 1) return null;
  return bands.findLast(band => total >= band.floor);
}

function browserStorage() {
  try { return globalThis.sessionStorage; } catch { return null; }
}

export function createReferencePicker(storage = browserStorage(), random = Math.random) {
  const decks = new Map();
  const views = new Map();
  function deckFor(band) {
    if (decks.has(band.floor)) return decks.get(band.floor);
    const key = `meter-reference-deck-v2-${band.floor}`;
    const size = band.items.length;
    let saved;
    try { saved = JSON.parse(storage?.getItem(key) ?? "null"); } catch { /* Storage is optional. */ }
    const valid = i => Number.isInteger(i) && i >= 0 && i < size;
    const left = saved?.left;
    const good = Array.isArray(left) && left.length < size && left.every(valid) &&
      new Set(left).size === left.length && valid(saved.last);
    const deck = {key, left: good ? left : [], last: good ? saved.last : -1};
    decks.set(band.floor, deck);
    return deck;
  }
  function deal(deck, size, previous) {
    if (!deck.left.length || (deck.left.length === 1 && deck.left[0] === previous)) {
      deck.left = Array.from({length: size}, (_, i) => i);
      for (let i = size - 1; i > 0; i--) {
        const j = Math.floor(random() * (i + 1));
        [deck.left[i], deck.left[j]] = [deck.left[j], deck.left[i]];
      }
    }
    if (deck.left.at(-1) === previous) {
      const end = deck.left.length - 1;
      [deck.left[0], deck.left[end]] = [deck.left[end], deck.left[0]];
    }
    return deck.left.pop();
  }
  return function choose(scope, total) {
    const band = tokenBand(total);
    if (!band) { views.delete(scope); return null; }
    const old = views.get(scope);
    const stable = old?.floor === band.floor;
    const pool = stable ? old.pool : (band.floor >= 80_000_000 && random() < 0.30 ? bands[5] : band);
    const deck = deckFor(pool);
    const index = stable ? old.index : deal(deck, pool.items.length, deck.last);
    if (deck.last !== index) {
      deck.last = index;
      try { storage?.setItem(deck.key, JSON.stringify({left: deck.left, last: index})); } catch { /* Storage is optional. */ }
    }
    views.delete(scope);
    views.set(scope, {floor: band.floor, pool, index});
    if (views.size > 12) views.delete(views.keys().next().value);
    const item = pool.items[index];
    const capacity = item.tokens;
    return {band, item, capacity, equivalents: total / capacity};
  };
}

export function referenceCaption(choice, partial = false) {
  if (!choice) return "";
  const {item, equivalents} = choice;
  const formatter = equivalents >= 10_000 ? largeQuantity : quantity;
  return `${partial ? "已知用量约合" : "约合"} ${formatter.format(equivalents)} ${item.unit}${item.name}`;
}

export function mostUsedModels(rows) {
  let maximum = 0;
  const leaders = new Set();
  for (const row of rows) {
    if (!row.model || /(?:^| · )型号未(?:记录|确认)$/.test(row.model)) continue;
    if (row.n > maximum) {
      maximum = row.n;
      leaders.clear();
    }
    if (row.n === maximum && maximum > 0) leaders.add(row);
  }
  return leaders;
}
