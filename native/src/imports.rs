use super::db::*;
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
fn count(v: &Value) -> Result<Option<i64>> {
    Ok(if v.is_null() {
        None
    } else {
        let n = v.as_i64().context("用量字段须为整数")?;
        ensure!((0..=9_000_000_000_000).contains(&n), "用量超出范围");
        Some(n)
    })
}
fn response(entry: &str, r: &Value) -> Result<Usage> {
    let u = &r["usage"];
    Ok(match entry {
        "openai-api" => {
            let (i, o) = if u.get("input_tokens").is_some() {
                ("input_tokens", "output_tokens")
            } else {
                ("prompt_tokens", "completion_tokens")
            };
            let ci = &u[format!("{i}_details")];
            Usage {
                total: count(&u["total_tokens"])?,
                input: count(&u[i])?,
                output: count(&u[o])?,
                cache_read: count(&ci["cached_tokens"])?,
                cache_write: count(&ci["cache_write_tokens"])?,
                reasoning: count(&u[format!("{o}_details")]["reasoning_tokens"])?,
            }
        }
        "claude-api" => {
            let read = count(&u["cache_read_input_tokens"])?;
            let write = count(&u["cache_creation_input_tokens"])?;
            Usage {
                input: count(&u["input_tokens"])?
                    .zip(read)
                    .zip(write)
                    .map(|((i, r), w)| i + r + w),
                output: count(&u["output_tokens"])?,
                cache_read: read,
                cache_write: write,
                ..Default::default()
            }
        }
        "gemini-api" | "ai-studio" => {
            let u = &r["usageMetadata"];
            let total = count(&u["totalTokenCount"])?;
            let tool = count(&u["toolUsePromptTokenCount"])?;
            let thought = count(&u["thoughtsTokenCount"])?;
            let mut i = count(&u["promptTokenCount"])?.map(|x| x + tool.unwrap_or(0));
            let mut o = count(&u["candidatesTokenCount"])?.map(|x| x + thought.unwrap_or(0));
            if i.zip(o).is_none_or(|(i, o)| Some(i + o) != total) {
                if tool.is_none() {
                    i = None
                }
                if thought.is_none() {
                    o = None
                }
            }
            Usage {
                total,
                input: i,
                output: o,
                cache_read: count(&u["cachedContentTokenCount"])?,
                reasoning: thought,
                ..Default::default()
            }
        }
        _ => anyhow::bail!("此入口需要标准逐请求格式"),
    })
}
fn convert(r: &Value) -> Result<Event> {
    let entry = string(r, "entry");
    let meta = &CATALOG["entries"][&entry];
    ensure!(meta[2] == "import", "入口不支持文件导入");
    let vendor = meta[1].as_str().unwrap();
    let mut model = string(r, "model");
    let mut rid = string(r, "request_id");
    let mut time = string(r, "timestamp");
    let source;
    let usage = match r["schema"].as_str() {
        Some("meter.usage.v1") => {
            source = "逐请求明细 · v1";
            ensure!(
                r.as_object()
                    .context("须为对象")?
                    .keys()
                    .all(|k| FIELDS.contains(&k.as_str())
                        || [
                            "schema",
                            "entry",
                            "request_id",
                            "project",
                            "timestamp",
                            "model"
                        ]
                        .contains(&k.as_str())),
                "标准记录含未知字段"
            );
            serde_json::from_value::<Usage>(r.clone())?
        }
        Some("meter.response.v1") => {
            ensure!(
                r["fictional"] != true && r["estimated"] != true,
                "不能导入估算或示例"
            );
            source = "厂商响应 · usage";
            let data = &r["response"];
            rid = data["id"]
                .as_str()
                .or(data["responseId"].as_str())
                .unwrap_or(&rid)
                .to_owned();
            model = data["modelVersion"]
                .as_str()
                .or(data["model"].as_str())
                .unwrap_or("")
                .to_owned();
            if time.is_empty() {
                if let Some(s) = data["createTime"].as_str() {
                    time = s.into()
                } else if let Some(t) = data["created_at"].as_i64().or(data["created"].as_i64()) {
                    time = chrono::DateTime::from_timestamp(t, 0)
                        .context("无效时间")?
                        .to_rfc3339()
                }
            }
            response(&entry, data)?
        }
        _ => anyhow::bail!("未知导入格式"),
    }
    .validate()?;
    let project = string(r, "project");
    ensure!(
        !rid.trim().is_empty()
            && rid.len() <= 1024
            && model.chars().count() <= 160
            && project.chars().count() <= 160,
        "请求 ID 或标签无效"
    );
    if entry == "gemini-api" || entry == "ai-studio" {
        model = model.trim_start_matches("models/").into()
    }
    Ok(Event {
        uid: identity(("api-request", vendor, rid.trim())),
        ts: stamp(&time)?,
        entry,
        vendor: vendor.into(),
        model: if model.is_empty() {
            "型号未记录".into()
        } else {
            model
        },
        project: if project.is_empty() {
            "未归类".into()
        } else {
            project
        },
        source: source.into(),
        usage,
    })
}
pub fn import(c: &mut Connection, raw: &[u8], fmt: &str, preview: bool) -> Result<Value> {
    let raw = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw);
    let rows = match fmt {
        "json" => {
            let doc: Value = serde_json::from_slice(raw)?;
            match doc {
                Value::Array(a) => a,
                v => vec![v],
            }
        }
        "csv" => {
            let mut rdr = csv::Reader::from_reader(raw);
            let headers = rdr.headers()?.clone();
            let mut rows = vec![];
            for r in rdr.records() {
                let mut v = json!({});
                for (k, s) in headers.iter().zip(r?.iter()) {
                    v[k] = if FIELDS.contains(&k) {
                        if s.is_empty() {
                            Value::Null
                        } else {
                            s.parse::<i64>()
                                .map(Value::from)
                                .unwrap_or_else(|_| json!(s))
                        }
                    } else {
                        json!(s)
                    }
                }
                rows.push(v)
            }
            rows
        }
        _ => anyhow::bail!("请选择 JSON 或 CSV"),
    };
    ensure!(!rows.is_empty(), "文件无记录");
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute("insert into batches(created) values(?)", [now()])?;
    let id = tx.last_insert_rowid();
    save(&tx, &format!("batch-sha:{id}"), &json!(hash(raw)))?;
    let mut result = json!({"added":0,"duplicate":0,"enriched":0,"conflict":0,"invalid":0,"problems":[],"batch":id});
    for (idx, r) in rows.iter().enumerate() {
        let mut issue = None;
        let outcome = match convert(r) {
            Err(e) => {
                issue = Some(e.to_string());
                "invalid"
            }
            Ok(e) => {
                let prior = get(&tx, &e.uid)?;
                match prior.as_ref().map(|o| merge(o, &e, false)).transpose() {
                    Err(e) => {
                        issue = Some(e.to_string());
                        "conflict"
                    }
                    Ok(merged) => {
                        let v = merged.as_ref().unwrap_or(&e);
                        let k = if prior.is_none() {
                            "added"
                        } else if prior.as_ref() == Some(v) {
                            "duplicate"
                        } else {
                            "enriched"
                        };
                        if k != "duplicate" {
                            put(&tx, v)?
                        }
                        tx.execute(
                            "insert into contributions values(?,?,?,?,?)",
                            params![id, idx + 1, e.uid, serde_json::to_string(&e)?, k],
                        )?;
                        k
                    }
                }
            }
        };
        result[outcome] = json!(result[outcome].as_u64().unwrap() + 1);
        if let Some(err) = issue {
            let p = result["problems"].as_array_mut().unwrap();
            if p.len() < 50 {
                p.push(json!(format!("第 {} 行 · {err}", idx + 1)))
            }
        }
    }
    if !preview {
        ensure!(
            result["invalid"] == 0 && result["conflict"] == 0,
            "{}；整批未入账",
            result["problems"][0]
        );
        tx.execute(
            "update batches set result=? where id=?",
            params![result.to_string(), id],
        )?;
        if result["added"] != 0 || result["enriched"] != 0 {
            bump(&tx)?
        }
        tx.commit()?
    }
    Ok(result)
}
pub fn undo(c: &mut Connection, id: i64) -> Result<Value> {
    let tx = c.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    ensure!(
        tx.execute(
            "update batches set undone=? where id=? and undone is null",
            params![now(), id]
        )? == 1,
        "批次已撤销或不存在"
    );
    let mut removed = 0;
    let mut retained = 0;
    let mut restored = 0;
    {
        let mut q = tx.prepare("select distinct uid from contributions where batch=?")?;
        for id in q.query_map([id], |r| r.get::<_, String>(0))? {
            let uid = id?;
            let mut keep = None;
            let mut s=tx.prepare("select payload from contributions join batches on batches.id=batch where uid=? and undone is null order by batch,line")?;
            for p in s.query_map([&uid], |r| r.get::<_, String>(0))? {
                let e: Event = serde_json::from_str(&p?)?;
                keep = Some(if let Some(k) = keep {
                    merge(&k, &e, false)?
                } else {
                    e
                })
            }
            if let Some(e) = keep {
                retained += 1;
                if get(&tx, &uid)?.as_ref() != Some(&e) {
                    put(&tx, &e)?;
                    restored += 1
                }
            } else {
                removed += tx.execute("delete from events where uid=?", [&uid])?
            }
        }
    }
    bump(&tx)?;
    tx.commit()?;
    Ok(json!({"removed":removed,"recomputed":restored,"retained":retained,"ok":true}))
}
