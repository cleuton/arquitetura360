use axum::{extract::State, routing::{post, get}, Json, Router};
use rand::Rng;
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    env,
    fs::OpenOptions,
    io::Write,
    net::SocketAddr,
    sync::{Arc, Mutex},
};
use tokio::{signal, time::{sleep, Duration}};

// ---------- CRDT LWW-Map ----------
#[derive(Clone)]
struct Value { ts: i64, node_id: u16, value: f64 }

#[derive(Serialize, Deserialize, Clone)]
struct Entry { key: String, ts: i64, node_id: u16, value: f64 }

#[derive(Serialize, Deserialize, Clone)]
struct GossipMsg { lww: Vec<Entry> }

struct Logger { file: Arc<Mutex<std::fs::File>> }
impl Logger {
    fn new(port: u16) -> Self {
        let _ = std::fs::create_dir_all("/logs");
        let f = OpenOptions::new().create(true).append(true)
            .open(format!("/logs/servidor_{}.log", port)).expect("open log");
        Self { file: Arc::new(Mutex::new(f)) }
    }
    fn log(&self, msg: &str) {
        let mut f = self.file.lock().unwrap();
        let line = format!("{} {}\n", chrono::Utc::now().to_rfc3339(), msg);
        let _ = f.write_all(line.as_bytes());
        let _ = f.flush();
    }
}

#[derive(Clone)]
struct AppState {
    map: Arc<Mutex<HashMap<String, Value>>>,
    logger: Arc<Logger>,
    peers: Arc<Vec<String>>,
    port: u16,
}

fn greater(a_ts: i64, a_n: u16, b_ts: i64, b_n: u16) -> bool {
    a_ts > b_ts || (a_ts == b_ts && a_n > b_n)
}

impl AppState {
    fn put(&self, key: String, ts: i64, node: u16, val: f64) {
        let mut m = self.map.lock().unwrap();
        match m.get(&key) {
            None => { m.insert(key, Value { ts, node_id: node, value: val }); }
            Some(cur) => if greater(ts, node, cur.ts, cur.node_id) {
                m.insert(key, Value { ts, node_id: node, value: val });
            }
        }
    }
    fn merge_many(&self, entries: Vec<Entry>) {
        let mut m = self.map.lock().unwrap();
        for e in entries {
            match m.get(&e.key) {
                None => { m.insert(e.key.clone(), Value { ts: e.ts, node_id: e.node_id, value: e.value }); }
                Some(cur) => if greater(e.ts, e.node_id, cur.ts, cur.node_id) {
                    m.insert(e.key.clone(), Value { ts: e.ts, node_id: e.node_id, value: e.value });
                }
            }
        }
    }
    fn snapshot(&self) -> HashMap<String, Value> { self.map.lock().unwrap().clone() }
}

fn group_by_device(state: &HashMap<String, Value>) -> HashMap<String, Vec<(String, f64, i64, u16)>> {
    let mut g: HashMap<String, Vec<(String, f64, i64, u16)>> = HashMap::new();
    for (k, v) in state.iter() {
        let (disp, met) = if let Some(i) = k.find(':') { (k[..i].to_string(), k[i+1..].to_string()) }
                          else { (k.clone(), "valor".to_string()) };
        g.entry(disp).or_default().push((met, v.value, v.ts, v.node_id));
    }
    g
}

fn disp_ord_key(d: &str) -> (u8, i64, String) {
    if let Some(rest) = d.strip_prefix("disp") {
        if let Ok(n) = rest.parse::<i64>() { return (0, n, d.to_string()); }
    }
    (1, i64::MAX, d.to_string())
}

fn log_estado(logger: &Logger, state: &HashMap<String, Value>, titulo: &str) {
    let mut grouped = group_by_device(state);
    logger.log(&format!("[ESTADO] {} — {} dispositivos", titulo, grouped.len()));
    let mut disps: Vec<_> = grouped.keys().cloned().collect();
    disps.sort_by_key(|d| disp_ord_key(d));
    for d in disps {
        let mut metrics = grouped.remove(&d).unwrap();
        metrics.sort_by(|a,b| a.0.cmp(&b.0));
        let parts: Vec<String> = metrics.into_iter()
            .map(|(met,val,ts,nid)| format!("{}={:.2}@{} nid={}", met, val, ts, nid))
            .collect();
        logger.log(&format!("[ESTADO] {}: {}", d, parts.join(", ")));
    }
}

// ---------- HTTP ----------
async fn gossip(State(state): State<AppState>, Json(msg): Json<GossipMsg>) -> Json<serde_json::Value> {
    state.merge_many(msg.lww.clone());
    state.logger.log(&format!("[REMOTO] Recebidas {} entradas", msg.lww.len()));
    let snap = state.snapshot();
    log_estado(&state.logger, &snap, "Após merge remoto");
    Json(serde_json::json!({"ok": true}))
}

async fn health() -> &'static str { "ok" }

// ---------- Workers ----------
async fn generate(state: AppState) {
    loop {
        for disp in 0..10 {
            for met in ["temperatura", "vibracao"] {
                let key = format!("disp{}:{}", disp, met);
                let ts = chrono::Utc::now().timestamp_millis();
                let val: f64 = rand::thread_rng().gen_range(0.0..100.0);
                state.put(key.clone(), ts, state.port, val);
                state.logger.log(&format!("[LOCAL] {} = {:.2} @ts={} nid={}", key, val, ts, state.port));
            }
        }
        let snap = state.snapshot();
        log_estado(&state.logger, &snap, "Após geração local");
        sleep(Duration::from_millis(500)).await;
    }
}

async fn disseminate(state: AppState) {
    let client = reqwest::Client::new();
    loop {
        let snap = state.snapshot();
        let payload = GossipMsg { lww: snap.into_iter().map(|(k,v)| Entry { key: k, ts: v.ts, node_id: v.node_id, value: v.value }).collect() };
        for p in state.peers.iter() {
            let url = format!("http://{}/gossip", p);
            if let Err(e) = client.post(&url).json(&payload).send().await {
                state.logger.log(&format!("[ERRO] Envio para {}: {}", p, e));
            }
        }
        sleep(Duration::from_secs(2)).await;
    }
}

// ---------- Main ----------
#[tokio::main]
async fn main() {
    let port: u16 = env::var("PORTA").ok().and_then(|s| s.parse().ok()).unwrap_or(7000);
    let peers: Vec<String> = env::var("COMPANHEIROS").unwrap_or_default().split(',')
        .map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();

    let state = AppState {
        map: Arc::new(Mutex::new(HashMap::new())),
        logger: Arc::new(Logger::new(port)),
        peers: Arc::new(peers),
        port,
    };

    let app = Router::new()
        .route("/gossip", post(gossip))
        .route("/healthz", get(health))
        .with_state(state.clone());

    {
        let st = state.clone();
        tokio::spawn(async move { generate(st).await; });
    }
    {
        let st = state.clone();
        tokio::spawn(async move { disseminate(st).await; });
    }

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    state.logger.log(&format!("[START] Rust na porta {}", port));
    let listener = tokio::net::TcpListener::bind(addr).await.expect("bind");
    axum::serve(listener, app)
        .with_graceful_shutdown(async { let _ = signal::ctrl_c().await; })
        .await
        .expect("server");
}
