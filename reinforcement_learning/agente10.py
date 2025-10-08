# Versão final: aprendizado de "hit-and-run" (atira e foge) + busca de vida e munição.
# Uso:
#   pip install pygame
#   python agente10.py --treinar --episodios 30000
#   python agente10.py --demo
# Arquivo salva política em qtable_final.pkl após treinar.

import os, random, pickle, argparse
from collections import defaultdict, deque, Counter
from typing import Dict, Tuple, Optional

# -------------------------
# Ambiente (grid simples)
# -------------------------
class JogoAcaoEnv:
    def __init__(self, tamanho=10, max_passos=360, semente: Optional[int]=None):
        self.N = tamanho
        self.max_passos = max_passos
        if semente is not None:
            random.seed(semente)

        # parâmetros de jogo
        self.custo_passo = -0.004
        self.dano_inimigo = 12
        self.dano_jogador = 11
        self.cura_kit = 25
        self.municao_caixa = 5
        self.reducao_cobertura = 0.55

        # comportamento inimigo
        self.cooldown_tiro_inimigo = 1
        self.cooldown_tiro_jogador = 1
        self.prob_tiro_inimigo = 0.58   # um pouco menos agressivo para dar janela
        self.prob_mov_inimigo = 0.78
        self.prob_mov_aleatorio_inimigo = 0.18

        # estado
        self.passos = 0
        self.vida = 100
        self.vida_inimigo = 100
        self.municao = 6
        self.em_cobertura = False
        self.cd_inimigo = 0
        self.cd_jogador = 0

        # memórias para shaping
        self.hist_pos = deque(maxlen=8)
        self.visitas = Counter()
        self.last_shot_tick = -999
        self.last_enemy_shot_tick = -999
        self.exposure_streak = 0

        # mapa
        self.paredes_base = set()
        self._reset_mapa_estatico()
        self.reset()

        # ações: 0..3 mover; 4 atirar; 5 ficar; 6 sprint (2 passos fugindo)
        self.acoes = 7

    def _reset_mapa_estatico(self):
        # cria uma "parede" vertical quebrada no meio (linha de cobertura)
        self.paredes_base = set()
        col = self.N // 2
        for r in range(1, self.N-1):
            if r % 2 == 0:
                self.paredes_base.add((r, col))

    def reset(self):
        self.passos = 0
        self.vida = 100
        self.vida_inimigo = 100
        self.municao = 6
        self.em_cobertura = False
        self.cd_inimigo = 0
        self.cd_jogador = 0
        self.paredes = set(self.paredes_base)
        # posições iniciais opostas
        self.jog_x, self.jog_y = 1, 1
        self.ini_x, self.ini_y = self.N-2, self.N-2
        # itens fixos para simplicidade
        self.kit_vida = (self.N-2, 1)
        self.caixa_municao = (1, self.N-2)
        self.hist_pos.clear(); self.visitas.clear()
        self.hist_pos.append((self.jog_x, self.jog_y)); self.visitas[(self.jog_x, self.jog_y)] += 1
        self.last_shot_tick = -999; self.last_enemy_shot_tick = -999
        self.exposure_streak = 0
        self._atualizar_cobertura()
        return self._estado()

    # estado observado (compacto)
    def _estado(self):
        return (
            self.jog_x, self.jog_y,
            self.ini_x, self.ini_y,
            int(self.em_cobertura),
            min(self.vida//10, 10),
            min(self.vida_inimigo//10, 10),
            min(self.municao, 9)
        )

    # utilitários de movimento e colisões
    def _livre(self, x, y, bloqueado: Optional[Tuple[int,int]]=None):
        if not (0 <= x < self.N and 0 <= y < self.N): return False
        if (x, y) in self.paredes: return False
        if bloqueado is not None and (x, y) == bloqueado: return False
        return True

    def _mover(self, x, y, acao, bloqueado: Optional[Tuple[int,int]]=None):
        dxdy = {0:(-1,0),1:(1,0),2:(0,-1),3:(0,1)}
        dx, dy = dxdy.get(acao, (0,0))
        nx, ny = x + dx, y + dy
        return (nx, ny) if self._livre(nx, ny, bloqueado=bloqueado) else (x, y)

    def _mover_duplo_melhorando_dist(self, x, y, alvox, alvoy, bloqueado=None):
        # tenta duas moves que maximizam distância ao alvo (usado pelo sprint)
        melhores = []
        melhor_gain = -10**9
        d0 = abs(x - alvox) + abs(y - alvoy)
        for a1 in (0,1,2,3):
            x1,y1 = self._mover(x,y,a1,bloqueado=bloqueado)
            for a2 in (0,1,2,3):
                x2,y2 = self._mover(x1,y1,a2,bloqueado=bloqueado)
                gain = (abs(x2-alvox)+abs(y2-alvoy)) - d0
                if (x2,y2) != (x,y):
                    if gain > melhor_gain:
                        melhor_gain = gain; melhores = [(x2,y2)]
                    elif gain == melhor_gain:
                        melhores.append((x2,y2))
        return random.choice(melhores) if melhores else (x,y)

    # linha de visada (linha reta sem paredes)
    def _linha_visada_livre(self, ax, ay, bx, by):
        if ax == bx:
            step = 1 if by > ay else -1
            for y in range(ay+step, by, step):
                if (ax,y) in self.paredes: return False
            return True
        if ay == by:
            step = 1 if bx > ax else -1
            for x in range(ax+step, bx, step):
                if (x,ay) in self.paredes: return False
            return True
        return False

    def _inimigo_tem_visada(self): return self._linha_visada_livre(self.ini_x, self.ini_y, self.jog_x, self.jog_y)
    def _jogador_tem_visada(self): return self._linha_visada_livre(self.jog_x, self.jog_y, self.ini_x, self.ini_y)

    def _adjacente_a_parede(self):
        viz = [(self.jog_x, self.jog_y-1),(self.jog_x, self.jog_y+1),
               (self.jog_x-1, self.jog_y),(self.jog_x+1, self.jog_y)]
        return any(v in self.paredes for v in viz)

    def _atualizar_cobertura(self):
        self.em_cobertura = self._adjacente_a_parede() and (not self._inimigo_tem_visada())

    def _corner_score(self, x, y):
        di = min(x, y, self.N-1-x, self.N-1-y)
        return (1 - di)  # 1 em canto, 0 no interior

    def _passo_inimigo(self):
        move_feito = False
        if random.random() < self.prob_mov_inimigo:
            candidatos = []
            melhor_d = 10**9
            for ac in (0,1,2,3):
                nx,ny = self._mover(self.ini_x, self.ini_y, ac, bloqueado=(self.jog_x, self.jog_y))
                if (nx,ny) != (self.ini_x, self.ini_y):
                    d = abs(nx - self.jog_x) + abs(ny - self.jog_y)
                    if d < melhor_d:
                        candidatos, melhor_d = [(nx,ny)], d
                    elif d == melhor_d:
                        candidatos.append((nx,ny))
            if candidatos:
                self.ini_x, self.ini_y = random.choice(candidatos)
                move_feito = True
        if (not move_feito) and random.random() < self.prob_mov_aleatorio_inimigo:
            for _ in range(4):
                ac = random.choice((0,1,2,3))
                nx,ny = self._mover(self.ini_x, self.ini_y, ac, bloqueado=(self.jog_x, self.jog_y))
                if (nx,ny) != (self.ini_x, self.ini_y):
                    self.ini_x, self.ini_y = nx,ny
                    break

    # passo do ambiente
    def step(self, acao):
        self.passos += 1
        recompensa, fim = 0.0, False

        # cooldowns
        if self.cd_inimigo > 0: self.cd_inimigo -= 1
        if self.cd_jogador > 0: self.cd_jogador -= 1

        vida_ini_antes = self.vida_inimigo
        visada_prev = self._inimigo_tem_visada()
        dist_prev = abs(self.ini_x - self.jog_x) + abs(self.ini_y - self.jog_y)
        corner_prev = self._corner_score(self.jog_x, self.jog_y)
        alinhado_prev = (self.jog_x == self.ini_x) or (self.jog_y == self.ini_y)

        # distâncias a itens (para shaping)
        dkv_prev = abs(self.kit_vida[0]-self.jog_x)+abs(self.kit_vida[1]-self.jog_y) if self.kit_vida else None
        dmn_prev = abs(self.caixa_municao[0]-self.jog_x)+abs(self.caixa_municao[1]-self.jog_y) if self.caixa_municao else None

        # --- ações do jogador ---
        if acao in (0,1,2,3):
            self.jog_x, self.jog_y = self._mover(self.jog_x, self.jog_y, acao, bloqueado=(self.ini_x, self.ini_y))
        elif acao == 4:
            # atirar
            if self.cd_jogador == 0 and self.municao > 0 and self._jogador_tem_visada():
                self.cd_jogador = self.cooldown_tiro_jogador
                self.municao -= 1
                self.vida_inimigo = max(0, self.vida_inimigo - self.dano_jogador)
                dano = vida_ini_antes - self.vida_inimigo
                if dano > 0:
                    recompensa += 0.65 + 0.14*dano  # recompensa por dano
                    self.last_shot_tick = self.passos
                if self.vida_inimigo == 0:
                    recompensa += 16.0
                    fim = True
            else:
                # atirar sem munição ou sem visada é ruim (leve penalidade)
                recompensa -= 0.015
        elif acao == 5:
            recompensa -= 0.04  # ficar parado levemente penalizado
        elif acao == 6:
            # sprint: tenta 2 passos aumentando distância
            nx,ny = self._mover_duplo_melhorando_dist(self.jog_x, self.jog_y, self.ini_x, self.ini_y, bloqueado=(self.ini_x, self.ini_y))
            self.jog_x, self.jog_y = nx, ny
            recompensa += 0.02  # leve incentivo a usar sprint quando relevante

        # coletar itens
        if self.kit_vida and (self.jog_x, self.jog_y) == self.kit_vida:
            self.vida = min(100, self.vida + self.cura_kit); self.kit_vida = None; recompensa += 1.0
        if self.caixa_municao and (self.jog_x, self.jog_y) == self.caixa_municao:
            self.municao += self.municao_caixa; self.caixa_municao = None; recompensa += 0.7

        # atualizar cobertura
        self._atualizar_cobertura()

        # --- ação do inimigo ---
        if not fim:
            if self._inimigo_tem_visada() and self.cd_inimigo == 0:
                if random.random() < self.prob_tiro_inimigo:
                    dano = self.dano_inimigo
                    if self.em_cobertura:
                        dano = int(dano * self.reducao_cobertura)
                    self.vida = max(0, self.vida - dano)
                    self.cd_inimigo = self.cooldown_tiro_inimigo
                    self.last_enemy_shot_tick = self.passos
                    if self.vida == 0:
                        recompensa -= 16.0
                        fim = True
                else:
                    self._passo_inimigo()
            else:
                self._passo_inimigo()

        # custo por passo
        recompensa += self.custo_passo

        # ---------------------------
        # shaping: reforços para hit-and-run e busca de itens
        # ---------------------------

        # (1) Penalidade crescente por exposição contínua (LoS sem cobertura)
        if self._inimigo_tem_visada() and (not self.em_cobertura):
            self.exposure_streak += 1
        else:
            self.exposure_streak = 0
        if self.exposure_streak > 0:
            recompensa -= 0.04 * min(self.exposure_streak, 6)  # cresce até um teto

        # (2) Pós-tiro do agente: 2 passos fortes para quebrar visada / aumentar distância / entrar em cobertura
        dt = self.passos - self.last_shot_tick
        if dt in (1, 2):
            # quebra de LoS imediatamente após atirar -> grande bônus
            if not self._inimigo_tem_visada():
                recompensa += 0.65
            # aumentar distância após atirar -> bom
            dist_now = abs(self.ini_x - self.jog_x) + abs(self.ini_y - self.jog_y)
            if dist_now > dist_prev:
                recompensa += 0.15
            # se entrou em cobertura -> bônus
            if self.em_cobertura:
                recompensa += 0.19

        # (3) Reação ao tiro do inimigo: se no passo seguinte quebrar LoS, recompensa
        dte = self.passos - self.last_enemy_shot_tick
        if dte == 1 and not self._inimigo_tem_visada():
            recompensa += 0.20

        # (4) Incentivar desalinhamento (sair da mesma linha/coluna)
        alinhado_now = (self.jog_x == self.ini_x) or (self.jog_y == self.ini_y)
        if alinhado_prev and (not alinhado_now):
            recompensa += 0.10
        if (not alinhado_prev) and alinhado_now and self._inimigo_tem_visada() and (not self.em_cobertura):
            recompensa -= 0.08

        # (5) Potencial para busca de itens (shaping denso)
        phi_prev = 0.0
        phi_now = 0.0
        if self.vida <= 65 and self.kit_vida:
            if dkv_prev is not None: phi_prev -= 0.6 * dkv_prev
            dkv_now = abs(self.kit_vida[0]-self.jog_x)+abs(self.kit_vida[1]-self.jog_y)
            phi_now -= 0.6 * dkv_now
        if self.municao <= 2 and self.caixa_municao:
            if dmn_prev is not None: phi_prev -= 0.5 * dmn_prev
            dmn_now = abs(self.caixa_municao[0]-self.jog_x)+abs(self.caixa_municao[1]-self.jog_y)
            phi_now -= 0.5 * dmn_now
        recompensa += 0.03 * (phi_now - phi_prev)  # delta de potencial pequeno mas denso

        # (6) penalidade por ficar preso/loop
        self.hist_pos.append((self.jog_x, self.jog_y)); self.visitas[(self.jog_x, self.jog_y)] += 1
        if len(self.hist_pos) >= 4:
            a,b,c,d = self.hist_pos[-4], self.hist_pos[-3], self.hist_pos[-2], self.hist_pos[-1]
            if a == c == d: recompensa -= 0.18
        if self.visitas[(self.jog_x, self.jog_y)] <= 2:
            recompensa += 0.02

        # (7) canto exposto é ruim
        corner_now = self._corner_score(self.jog_x, self.jog_y)
        if corner_now >= 1 and (not self.em_cobertura) and self._inimigo_tem_visada():
            recompensa -= 0.6
        delta_corner = corner_prev - corner_now
        if delta_corner > 0:
            recompensa += 0.09 * delta_corner

        # fim por passos
        if self.passos >= self.max_passos: fim = True
        return self._estado(), recompensa, fim, {}

# -------------------------
# Agente Q-learning tabular simples
# -------------------------
class AgenteQLearning:
    def __init__(self, n_acoes, alfa=0.12, gama=0.98, epsilon=1.0, epsilon_min=0.04, decaimento=0.99994):
        self.n_acoes = n_acoes
        self.alfa = alfa
        self.gama = gama
        self.epsilon = epsilon
        self.epsilon_min = epsilon_min
        self.decaimento = decaimento
        self.Q: Dict[Tuple, float] = defaultdict(float)

    def escolher_acao(self, estado):
        if random.random() < self.epsilon:
            return random.randrange(self.n_acoes)
        melhor_a, melhor_q = 0, float("-inf")
        for a in range(self.n_acoes):
            q = self.Q[(estado, a)]
            if q > melhor_q:
                melhor_q, melhor_a = q, a
        return melhor_a

    def atualizar(self, s, a, r, s2, fim):
        qsa = self.Q[(s, a)]
        if fim:
            alvo = r
        else:
            max_q = max(self.Q[(s2, a2)] for a2 in range(self.n_acoes))
            alvo = r + self.gama * max_q
        self.Q[(s, a)] = qsa + self.alfa * (alvo - qsa)

    def decair_exploracao(self):
        self.epsilon = max(self.epsilon_min, self.epsilon * self.decaimento)

# -------------------------
# utilitários salvar/carregar
# -------------------------
def salvar_politica(agente, caminho="qtable_final.pkl"):
    with open(caminho, "wb") as f:
        pickle.dump(dict(agente.Q), f)

def carregar_politica(agente, caminho="qtable_final.pkl"):
    if os.path.exists(caminho):
        with open(caminho, "rb") as f:
            agente.Q.update(pickle.load(f))
        return True
    return False

# -------------------------
# treino
# -------------------------
def treinar(episodios=30000, tamanho=10, semente=42,
            alfa=0.12, gama=0.98, epsilon=1.0, epsilon_min=0.04, decaimento=0.99994,
            max_passos=360, janela_media=300, log_cada=300):
    env = JogoAcaoEnv(tamanho=tamanho, max_passos=max_passos, semente=semente)
    agente = AgenteQLearning(n_acoes=env.acoes, alfa=alfa, gama=gama,
                             epsilon=epsilon, epsilon_min=epsilon_min, decaimento=decaimento)
    fila = deque(maxlen=janela_media)
    wins = deque(maxlen=log_cada)
    for ep in range(1, episodios+1):
        s = env.reset(); total = 0.0; fim = False; matou = False
        while not fim:
            a = agente.escolher_acao(s)
            s2, r, fim, _ = env.step(a)
            # bucket vida_inimigo == 0 -> s2[6] == 0 (vida_inimigo bucket)
            if s2[6] == 0: matou = True
            agente.atualizar(s, a, r, s2, fim)
            s = s2; total += r
        agente.decair_exploracao()
        fila.append(total); wins.append(1 if matou else 0)
        if ep % log_cada == 0:
            media = sum(fila)/len(fila) if fila else 0.0
            wr = sum(wins)/len(wins) if wins else 0.0
            print(f"[ep {ep}] média {media:.3f} | win-rate {wr:.3f} | epsilon {agente.epsilon:.3f}")
    salvar_politica(agente, "qtable_final.pkl")
    return env, agente

# -------------------------
# Demo pygame com projéteis e toast
# -------------------------
def rodar_demo_pygame(tamanho=10, max_passos=360, framerate=12, seed=7, qtable_path="qtable_final.pkl", epsilon_demo=0.05):
    try:
        import pygame
    except Exception:
        print("Pygame não está instalado. Instale com: pip install pygame")
        return

    TAM, M = 48, 14
    C = {
        "fundo": (18,18,22),"grid":(40,40,50),"parede":(100,100,120),
        "player":(60,170,240),"enemy":(230,80,80),"vida":(40,220,120),
        "municao":(250,210,80),"texto":(230,230,240),"cobertura":(120,200,255),
        "bala_player":(120,220,255),"bala_enemy":(255,170,170),"toast_bg":(30,30,40),
    }

    def rcel(x,y): return (M + y*TAM, M + x*TAM, TAM, TAM)
    def ccenter(x,y): return (M + y*TAM + TAM//2, M + x*TAM + TAM//2)

    def draw_grid(scr,N):
        for i in range(N+1):
            x = M + i*TAM; y0 = M; y1 = M + N*TAM
            pygame.draw.line(scr, C["grid"], (x,y0),(x,y1), 1)
            y = M + i*TAM; x0 = M; x1 = M + N*TAM
            pygame.draw.line(scr, C["grid"], (x0,y),(x1,y), 1)

    def animar_projeteis(scr, env, balas, framerate=12):
        frames = 8
        for f in range(frames):
            scr.fill(C["fundo"]); draw_grid(scr, env.N)
            for (x,y) in env.paredes: pygame.draw.rect(scr, C["parede"], rcel(x,y))
            if env.kit_vida: pygame.draw.rect(scr, C["vida"], rcel(*env.kit_vida))
            if env.caixa_municao: pygame.draw.rect(scr, C["municao"], rcel(*env.caixa_municao))
            if env.em_cobertura:
                pygame.draw.rect(scr, C["cobertura"], pygame.Rect(*rcel(env.jog_x, env.jog_y)).inflate(8,8), 3)
            pygame.draw.rect(scr, C["player"], rcel(env.jog_x, env.jog_y))
            pygame.draw.rect(scr, C["enemy"], rcel(env.ini_x, env.ini_y))
            for start, end, cor in balas:
                t = (f + 1) / frames
                x = start[0] + (end[0]-start[0]) * t
                y = start[1] + (end[1]-start[1]) * t
                pygame.draw.circle(scr, cor, (int(x), int(y)), 6)
            hud = f"Vida:{env.vida}  Muni:{env.municao}  NPC:{env.vida_inimigo}  LoS:{int(env._inimigo_tem_visada())}  Cob:{int(env.em_cobertura)}"
            scr.blit(pygame.font.SysFont("DejaVu Sans",18).render(hud,True,C["texto"]), (M, M + env.N*TAM + 8))
            pygame.display.flip(); pygame.time.delay(int(1000/(framerate*1.5)))

    def toast(scr, fonte, msg, duration_ms=800):
        text = fonte.render(msg, True, (255,255,255))
        pad = 12
        box = pygame.Surface((text.get_width()+pad*2, text.get_height()+pad*2), pygame.SRCALPHA)
        pygame.draw.rect(box, (*C["toast_bg"], 210), box.get_rect(), border_radius=10)
        box.blit(text, (pad, pad)); rect = box.get_rect(center=scr.get_rect().center)
        t0 = pygame.time.get_ticks()
        while pygame.time.get_ticks() - t0 < duration_ms:
            scr.blit(box, rect); pygame.display.flip(); pygame.time.delay(20)

    pygame.init()
    fonte = pygame.font.SysFont("DejaVu Sans", 18)
    scr = pygame.display.set_mode((M*2 + tamanho*TAM, M*2 + tamanho*TAM + 46))
    pygame.display.set_caption("Agente Shooter Reinforcement Learning")
    clock = pygame.time.Clock()

    env = JogoAcaoEnv(tamanho=tamanho, max_passos=max_passos, semente=seed)
    ag = AgenteQLearning(n_acoes=env.acoes)
    if os.path.exists(qtable_path):
        with open(qtable_path,"rb") as f:
            ag.Q.update(pickle.load(f))

    def argmax_eps(ag, s, n, eps):
        if random.random() < eps: return random.randrange(n)
        best_a, best_q = 0, float("-inf")
        for a in range(n):
            q = ag.Q[(s,a)]
            if q > best_q: best_q, best_a = q, a
        return best_a

    s = env.reset()
    vida_prev, vida_npc_prev = env.vida, env.vida_inimigo
    running = True
    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT: running = False
            elif e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE: running = False

        cpb = ccenter(env.jog_x, env.jog_y)
        ceb = ccenter(env.ini_x, env.ini_y)
        los_inimigo_antes = env._inimigo_tem_visada()
        los_jogador_antes = env._jogador_tem_visada()

        a = argmax_eps(ag, s, env.acoes, epsilon_demo)
        s, r, fim, _ = env.step(a)

        # render base
        scr.fill(C["fundo"]); draw_grid(scr, env.N)
        for (x,y) in env.paredes: pygame.draw.rect(scr, C["parede"], rcel(x,y))
        if env.kit_vida: pygame.draw.rect(scr, C["vida"], rcel(*env.kit_vida))
        if env.caixa_municao: pygame.draw.rect(scr, C["municao"], rcel(*env.caixa_municao))
        if env.em_cobertura:
            pygame.draw.rect(scr, C["cobertura"], pygame.Rect(*rcel(env.jog_x, env.jog_y)).inflate(8,8), 3)
        pygame.draw.rect(scr, C["player"], rcel(env.jog_x, env.jog_y))
        pygame.draw.rect(scr, C["enemy"], rcel(env.ini_x, env.ini_y))
        hud = f"Vida:{env.vida}  Muni:{env.municao}  NPC:{env.vida_inimigo}  LoS:{int(env._inimigo_tem_visada())}  Cob:{int(env.em_cobertura)}  Passo:{env.passos}"
        scr.blit(fonte.render(hud, True, C["texto"]), (M, M + env.N*TAM + 8))
        pygame.display.flip()

        # projéteis animados: se houve dano no passo e tinha LoS antes, anima entre centros
        balas = []
        if env.vida < vida_prev and los_inimigo_antes:
            balas.append((ceb, cpb, C["bala_enemy"]))
        if env.vida_inimigo < vida_npc_prev and los_jogador_antes:
            balas.append((cpb, ceb, C["bala_player"]))
        if balas:
            animar_projeteis(scr, env, balas, framerate=framerate)

        # toasts de morte
        if env.vida <= 0 < vida_prev:
            toast(scr, fonte, "Agente RL morreu")
        elif env.vida_inimigo <= 0 < vida_npc_prev:
            toast(scr, fonte, "Inimigo morreu")

        if fim:
            pygame.display.flip(); pygame.time.delay(300)
            s = env.reset(); vida_prev, vida_npc_prev = env.vida, env.vida_inimigo
            continue

        vida_prev, vida_npc_prev = env.vida, env.vida_inimigo
        clock.tick(framerate)

# -------------------------
# CLI
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--treinar", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--episodios", type=int, default=30000)
    ap.add_argument("--tamanho", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--qtable", type=str, default="qtable_final.pkl")
    args = ap.parse_args()

    global max_passos, epsilon_demo, qtable_path
    max_passos = 360
    epsilon_demo = 0.04
    qtable_path = args.qtable

    if args.treinar:
        env, ag = treinar(episodios=args.episodios, tamanho=args.tamanho, semente=args.seed)
        salvar_politica(ag, args.qtable)
    if args.demo:
        rodar_demo_pygame(tamanho=args.tamanho, max_passos=max_passos, seed=args.seed, qtable_path=args.qtable)

    if not args.treinar and not args.demo:
        print("Use: python agente10.py --treinar | --demo")

if __name__ == "__main__":
    main()
