use crate::db::*;
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use std::{collections::BTreeMap, path::Path};
#[derive(Clone, Copy)]
enum Wire<'a> {
    Int(u64),
    Bytes(&'a [u8]),
}
fn var(b: &[u8], p: &mut usize) -> Result<u64> {
    let mut n = 0;
    for shift in (0..=63).step_by(7) {
        let v = *b.get(*p).context("不完整的元数据")?;
        *p += 1;
        ensure!(shift < 63 || v <= 1, "整数溢出");
        n |= ((v & 127) as u64) << shift;
        if v < 128 {
            return Ok(n);
        }
    }
    anyhow::bail!("整数溢出")
}
fn fields<'a>(b: &'a [u8], want: &[u64]) -> Result<BTreeMap<u64, Wire<'a>>> {
    let mut out = BTreeMap::new();
    let mut p = 0;
    while p < b.len() {
        let tag = var(b, &mut p)?;
        ensure!(tag >> 3 != 0, "字段编号无效");
        let value = match tag & 7 {
            0 => Wire::Int(var(b, &mut p)?),
            kind @ (1 | 2 | 5) => {
                let len = match kind {
                    1 => 8,
                    5 => 4,
                    _ => usize::try_from(var(b, &mut p)?)?,
                };
                let end = p.checked_add(len).context("无效字段")?;
                let v = b.get(p..end).context("元数据不完整")?;
                p = end;
                Wire::Bytes(v)
            }
            _ => anyhow::bail!("元数据类型未知"),
        };
        if want.contains(&(tag >> 3)) {
            out.insert(tag >> 3, value);
        }
    }
    Ok(out)
}
impl Wire<'_> {
    fn int(self) -> Result<i64> {
        if let Wire::Int(v) = self {
            Ok(v.try_into()?)
        } else {
            anyhow::bail!("字段类型不符")
        }
    }
}
fn bytes<'a>(v: Option<&Wire<'a>>) -> Option<&'a [u8]> {
    if let Some(Wire::Bytes(b)) = v {
        Some(b)
    } else {
        None
    }
}
#[derive(Clone, PartialEq)]
struct Stats {
    rid: Option<String>,
    code: i64,
    u: Usage,
}
fn stats(b: &[u8]) -> Result<Option<Stats>> {
    let f = fields(b, &[1, 2, 3, 4, 5, 9, 11])?;
    if !(2..=5).any(|i| f.contains_key(&i)) {
        return Ok(None);
    }
    let n = |i| f.get(&i).copied().map(Wire::int).transpose();
    let read = n(5)?.unwrap_or(0);
    let write = n(4)?.unwrap_or(0);
    let input = n(2)?
        .unwrap_or(0)
        .checked_add(read)
        .and_then(|x| x.checked_add(write))
        .context("输入溢出")?;
    Ok(Some(Stats {
        rid: bytes(f.get(&11))
            .filter(|b| !b.is_empty())
            .map(|b| b.iter().map(|x| format!("{x:02x}")).collect()),
        code: n(1)?.unwrap_or(0),
        u: Usage {
            input: Some(input),
            output: Some(n(3)?.unwrap_or(0)),
            cache_read: Some(read),
            cache_write: Some(write),
            reasoning: n(9)?,
            total: None,
        }
        .validate()?,
    }))
}
struct Obs {
    key: &'static str,
    idx: i64,
    src: &'static str,
    slot: i64,
    ts: i64,
    s: Stats,
    name: Option<String>,
}
pub fn read(db: &Connection, path: &Path, entry: &str, origin: &str) -> Result<(usize, usize)> {
    let src = crate::discovery::readonly(path)?;
    src.execute_batch("pragma query_only=ON; begin")?;
    let trajectory = src
        .query_row(
            "select trajectory_id from trajectory_meta limit 1",
            [],
            |r| r.get::<_, Option<String>>(0),
        )
        .optional()?
        .flatten()
        .unwrap_or_else(|| {
            path.file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .into()
        });
    let mut project_name = "未归类".to_owned();
    if let Ok(mut q) = src.prepare("select data from trajectory_metadata_blob") {
        let mut names = std::collections::BTreeSet::new();
        for b in q.query_map([], |r| r.get::<_, Vec<u8>>(0))? {
            let b = b?;
            if let Some(ws) = bytes(fields(&b, &[1])?.get(&1)) {
                if let Some(uri) = bytes(fields(ws, &[1])?.get(&1))
                    .and_then(|s| std::str::from_utf8(s).ok())
                    .and_then(|s| s.strip_prefix("file:///"))
                {
                    let decoded = form_urlencoded::parse(
                        format!("p={}", uri.replace('+', "%2B").replace('&', "%26")).as_bytes(),
                    )
                    .next()
                    .map(|(_, v)| v.into_owned())
                    .unwrap_or_default();
                    names.insert(project(db, &decoded)?);
                }
            }
        }
        if names.len() == 1 {
            project_name = names.into_iter().next().unwrap()
        }
    }
    let mut times = BTreeMap::new();
    let mut steps = BTreeMap::new();
    let mut req = BTreeMap::new();
    let mut missed = 0;
    let mut q =
        src.prepare("select idx,metadata from steps where metadata is not null order by idx")?;
    for row in q.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?)))? {
        let (i, b) = row?;
        let read = (|| -> Result<()> {
            let m = fields(&b, &[1, 9])?;
            if let Some(t) = bytes(m.get(&1)) {
                let f = fields(t, &[1, 2])?;
                let sec = f.get(&1).copied().map(Wire::int).transpose()?.unwrap_or(0);
                let ns = f.get(&2).copied().map(Wire::int).transpose()?.unwrap_or(0);
                if (1..7_290_000_000).contains(&sec) && (0..1_000_000_000).contains(&ns) {
                    times.insert(i, sec * 1_000_000 + ns / 1000);
                }
            }
            if let Some(s) = bytes(m.get(&9)).map(stats).transpose()?.flatten() {
                if let Some(id) = &s.rid {
                    req.insert(id.clone(), i);
                }
                steps.insert(i, s);
            }
            Ok(())
        })();
        if read.is_err() {
            missed += 1
        }
    }
    let mut extra = Vec::new();
    let mut names = BTreeMap::new();
    let mut q =
        src.prepare("select idx,data from gen_metadata where data is not null order by idx")?;
    for row in q.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Vec<u8>>(1)?)))? {
        let (slot, b) = row?;
        let res = (|| -> Result<()> {
            let out = fields(&b, &[1, 2])?;
            let Some(g) = bytes(out.get(&1)) else {
                return Ok(());
            };
            let gen = fields(g, &[4, 19])?;
            let Some(s) = bytes(gen.get(&4)).map(stats).transpose()?.flatten() else {
                return Ok(());
            };
            let name = bytes(gen.get(&19))
                .and_then(|b| std::str::from_utf8(b).ok())
                .filter(|s| {
                    !s.is_empty()
                        && s.len() <= 150
                        && s.bytes()
                            .all(|c| c.is_ascii_alphanumeric() || b"._:/-".contains(&c))
                })
                .map(str::to_owned);
            let mut indices = Vec::new();
            match out.get(&2) {
                Some(Wire::Bytes(b)) => {
                    let mut p = 0;
                    while p < b.len() {
                        indices.push(i64::try_from(var(b, &mut p)?)?)
                    }
                }
                Some(v) => indices.push(v.int()?),
                None => (),
            }
            let mut matched = s.rid.as_ref().and_then(|r| req.get(r)).copied();
            if matched.is_none() {
                let eligible: std::collections::BTreeSet<_> = indices
                    .iter()
                    .filter(|i| {
                        steps
                            .get(i)
                            .is_some_and(|t| t.rid.is_none() || s.rid.is_none())
                    })
                    .copied()
                    .collect();
                let exact: Vec<_> = eligible
                    .iter()
                    .filter(|i| steps[i].code == s.code && steps[i].u == s.u)
                    .collect();
                if exact.len() == 1 {
                    matched = Some(*exact[0])
                } else {
                    ensure!(eligible.is_empty(), "无法唯一关联步骤")
                }
            }
            let (key, idx, ts) = if let Some(i) = matched {
                let st = steps.get_mut(&i).unwrap();
                if st.rid.is_none() {
                    st.rid = s.rid.clone()
                }
                if st.code == s.code && st.u == s.u && name.is_some() {
                    names.insert(i, name.clone());
                }
                ("step", i, *times.get(&i).context("缺少步骤时间")?)
            } else {
                (
                    "generation",
                    slot,
                    *indices
                        .iter()
                        .find_map(|i| times.get(i))
                        .context("缺少生成时间")?,
                )
            };
            extra.push(Obs {
                key,
                idx,
                src: "AGY · gen_metadata",
                slot,
                ts,
                s,
                name,
            });
            Ok(())
        })();
        if res.is_err() {
            missed += 1
        }
    }
    let mut obs = Vec::new();
    for (i, s) in steps {
        if let Some(&ts) = times.get(&i) {
            obs.push(Obs {
                key: "step",
                idx: i,
                src: "AGY · steps.metadata",
                slot: i,
                ts,
                s,
                name: names.remove(&i).flatten(),
            })
        } else {
            missed += 1
        }
    }
    obs.extend(extra);
    let mut changed = 0;
    for o in obs {
        let anchor = identity(("agy-local", &trajectory, o.key, o.idx));
        let requested = o.s.rid.map(|r| identity(("agy-request", r)));
        let lookup = |k: &str| {
            db.query_row("select uid from anchors where anchor=?", [k], |r| {
                r.get::<_, String>(0)
            })
            .optional()
        };
        let known: Vec<String> = {
            let mut q = db.prepare(
                "select distinct uid from anchors where anchor=? or anchor like ? limit 2",
            )?;
            let found = q
                .query_map(params![anchor, format!("{anchor}:%")], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            found
        };
        let uid = if let Some(ref request) = requested {
            let resolved = lookup(request)?.unwrap_or_else(|| {
                if known.len() == 1
                    && known[0] == anchor
                    && !db
                        .query_row(
                            "select exists(select 1 from anchors where anchor like ?)",
                            [format!("{anchor}:%")],
                            |r| r.get::<_, bool>(0),
                        )
                        .unwrap_or(true)
                {
                    anchor.clone()
                } else {
                    request.clone()
                }
            });
            db.execute(
                "insert or ignore into anchors values(?,?)",
                params![request, resolved],
            )?;
            db.execute(
                "insert or ignore into anchors values(?,?)",
                params![format!("{anchor}:{request}"), resolved],
            )?;
            resolved
        } else {
            if known.len() > 1 {
                missed += 1;
                continue;
            }
            let id = known.first().cloned().unwrap_or_else(|| anchor.clone());
            db.execute(
                "insert or ignore into anchors values(?,?)",
                params![anchor, id],
            )?;
            id
        };
        let model = o.name.unwrap_or_else(|| {
            if o.s.code == 0 {
                "AGY · 型号未记录".into()
            } else {
                format!("AGY #{} · 型号未确认", o.s.code)
            }
        });
        let e = Event {
            uid: uid.clone(),
            ts: o.ts,
            entry: entry.into(),
            vendor: vendor(&model, "厂商未确认"),
            model,
            source: o.src.into(),
            usage: o.s.u,
            project: project_name.clone(),
        };
        let prior = get(db, &uid)?;
        let location = format!("{origin}:{}:{}", o.src, o.slot);
        let payload = serde_json::to_string(&e)?;
        db.execute(
            "insert or ignore into origins values(?,?,?)",
            params![uid, location, payload],
        )?;
        let obs: i64 = db.query_row(
            "select rowid from origins where uid=? and origin=? and payload=?",
            params![uid, location, payload],
            |r| r.get(0),
        )?;
        if db.execute("insert into heads values(?,?,?) on conflict(uid,origin) do update set evidence=excluded.evidence where evidence<>excluded.evidence",params![uid,location,obs])?==0{continue}

        let mut effective = e.clone();
        if let Some(old) = &prior {
            effective = match merge(old, &e, true) {
                Ok(v) => v,
                Err(err) => {
                    db.execute(
                        "insert or replace into conflicts values(?,?)",
                        params![uid, err.to_string()],
                    )?;
                    old.clone()
                }
            };
            if old.entry != entry {
                effective.entry = "agy-shared".into()
            }
        }
        if prior.as_ref() != Some(&effective) {
            put(db, &effective)?;
            changed += 1
        }
    }
    Ok((changed, missed))
}
