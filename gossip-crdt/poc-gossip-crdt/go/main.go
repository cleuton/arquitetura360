// Servidor P2P (Go) - Gossip + CRDT LWW-Map - Logs detalhados de convergência + /healthz
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type Item struct {
	TS     int64
	NodeID int
	Value  float64
}
type Entry struct {
	Key    string  `json:"key"`
	TS     int64   `json:"ts"`
	NodeID int     `json:"node_id"`
	Value  float64 `json:"value"`
}
type Gossip struct {
	LWW []Entry `json:"lww"`
}

var (
	state  = map[string]Item{}
	logger *log.Logger
	porta  int
)

func maior(aTS int64, aN int, bTS int64, bN int) bool { return aTS > bTS || (aTS == bTS && aN > bN) }
func put(key string, ts int64, node int, val float64) {
	cur, ok := state[key]
	if !ok || maior(ts, node, cur.TS, cur.NodeID) {
		state[key] = Item{TS: ts, NodeID: node, Value: val}
	}
}
func mergeMany(entries []Entry) {
	for _, e := range entries {
		cur, ok := state[e.Key]
		if !ok || maior(e.TS, e.NodeID, cur.TS, cur.NodeID) {
			state[e.Key] = Item{TS: e.TS, NodeID: e.NodeID, Value: e.Value}
		}
	}
}
func snapshot() map[string]Item {
	out := make(map[string]Item, len(state))
	for k, v := range state {
		out[k] = v
	}
	return out
}

func logEstado(titulo string) {
	s := snapshot()
	type Met struct {
		Name string
		Val  float64
		TS   int64
		NID  int
	}
	group := map[string][]Met{}
	for k, it := range s {
		disp := k
		met := "valor"
		if idx := strings.IndexByte(k, ':'); idx >= 0 {
			disp = k[:idx]
			met = k[idx+1:]
		}
		group[disp] = append(group[disp], Met{met, it.Value, it.TS, it.NodeID})
	}
	logger.Printf("[ESTADO] %s — %d dispositivos", titulo, len(group))
	disps := make([]string, 0, len(group))
	for d := range group {
		disps = append(disps, d)
	}
	sort.Slice(disps, func(i, j int) bool {
		di := strings.TrimPrefix(disps[i], "disp")
		dj := strings.TrimPrefix(disps[j], "disp")
		ii, _ := strconv.Atoi(di)
		jj, _ := strconv.Atoi(dj)
		return ii < jj
	})
	for _, d := range disps {
		metrics := group[d]
		sort.Slice(metrics, func(i, j int) bool { return metrics[i].Name < metrics[j].Name })
		parts := []string{}
		for _, m := range metrics {
			parts = append(parts, fmt.Sprintf("%s=%.2f@%d nid=%d", m.Name, m.Val, m.TS, m.NID))
		}
		logger.Printf("[ESTADO] %s: %s", d, strings.Join(parts, ", "))
	}
}

func handleGossip(w http.ResponseWriter, r *http.Request) {
	var g Gossip
	if err := json.NewDecoder(r.Body).Decode(&g); err != nil {
		w.WriteHeader(400)
		return
	}
	mergeMany(g.LWW)
	logger.Printf("[REMOTO] Recebidas %d entradas", len(g.LWW))
	logEstado("Após merge remoto")
	w.WriteHeader(200)
}

func handleHealth(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) }

func gerarMetricas() {
	for {
		for disp := 0; disp < 10; disp++ {
			for _, met := range []string{"temperatura", "vibracao"} {
				key := fmt.Sprintf("disp%d:%s", disp, met)
				ts := time.Now().UnixMilli()
				val := float64(rand.Intn(101))
				put(key, ts, porta, val)
				logger.Printf("[LOCAL] %s = %.2f @ts=%d nid=%d", key, val, ts, porta)
			}
		}
		logEstado("Após geração local")
		time.Sleep(500 * time.Millisecond)
	}
}

func disseminar(companheiros []string) {
	client := &http.Client{Timeout: 3 * time.Second}
	for {
		s := snapshot()
		lww := make([]Entry, 0, len(s))
		for k, v := range s {
			lww = append(lww, Entry{Key: k, TS: v.TS, NodeID: v.NodeID, Value: v.Value})
		}
		body, _ := json.Marshal(Gossip{LWW: lww})
		for _, c := range companheiros {
			_, err := client.Post("http://"+c+"/gossip", "application/json", bytes.NewReader(body))
			if err != nil {
				logger.Printf("[ERRO] Envio gossip para %s: %v", c, err)
			}
		}
		time.Sleep(2 * time.Second)
	}
}

func main() {
	if p, _ := strconv.Atoi(os.Getenv("PORTA")); p > 0 {
		porta = p
	} else {
		porta = 6000
	}
	companheiros := []string{}
	if s := os.Getenv("COMPANHEIROS"); s != "" {
		for _, p := range strings.Split(s, ",") {
			p = strings.TrimSpace(p)
			if p != "" {
				companheiros = append(companheiros, p)
			}
		}
	}
	_ = os.MkdirAll("/logs", 0755)
	logPath := filepath.Join("/logs", fmt.Sprintf("servidor_%d.log", porta))
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0666)
	if err != nil {
		panic(err)
	}
	syscall.Dup2(int(f.Fd()), 1)
	syscall.Dup2(int(f.Fd()), 2)
	logger = log.New(f, "", log.LstdFlags)
	logger.Printf("[START] Go na porta %d", porta)

	http.HandleFunc("/gossip", handleGossip)
	http.HandleFunc("/healthz", handleHealth)

	go gerarMetricas()
	go disseminar(companheiros)

	if err := http.ListenAndServe(fmt.Sprintf(":%d", porta), nil); err != nil {
		logger.Printf("[ERRO] HTTP: %v", err)
	}
}
