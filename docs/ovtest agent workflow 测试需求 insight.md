A. Code over YAML/Config
- code is cheap,code 可能比 yaml 好;人不读太多代码,只从代码里拿 interpretation,其余靠注释/judge。
- 用静态语言(Go)做调度 & test execution。
- 先纯 Go: case = Go struct 字面量(数据);「schema 在 Go 代码里也只是个类似 plaintext 的定义」;先不考虑 config 混合,未来有必要再改。
- 自建 Go / Python 流程引擎更能满足过程跟踪; 复用 Dedup Handler 流程引擎可能也是一种方案
  
B. Trace / 中间过程 / Evidence
- 一条 trace 跑完,最终很可能要 judge 很多中间过程。[程序化判断 early exit, LLM 语义化判断]
- 中间结果要逐级传递到最终 judge, 可以是 by case 外挂存储
  - -- pydagflow 变量
  
C. LLM 只放在两个「可收束、可验证」环节
1. test case / workflow 构建(writer)
2. LLM judge
- 「每次需要稳定运行」的测试过程放进固定的、生成后的 workflow。不由 agent 根据 skill 自由发挥。

D. Judge 设计
- 一个 test case 一个 judge 是 ok 的, 且每个 judge 只 eval 一个具体的 trace/case。
- 单 judge,但支持 per node / layer 的 deterministic check(前置到 execution node 里检查并可 early exit);per node 放 agent judge 没必要。
- 不会「所有 case 全局只跑一次 judge」——token window 扛不住。
- 最终产出无法被固定流程(keyword match) review, 必须 judge agent 按自然语言参考(NL reference)验证。
- judge 判断不可能是确定性的(否则就跟 keyword match 没区别), 但功能性验证一定是简单, 稳定成功的 (multi-pass should success)。
- judge 输入:纯 NL reference,输出 {pass/fail, 理由}(不要结构化 rubric 清单)。
  
E. Writer(测试定义者)
- 创建 test 时发挥想象、验证重复性、cover 尽可能多 edge case。
- LLM 擅长写可验证的代码并在 loop 中验证; writer 负责 loop 后的首次环境内自验证。
- 澄清: writer 自己先写出来 → 人工 eval 看效果没问题再提交; eval 的内容仍可被人/agent 评估是否有真实价值; 不是把 writer 跑出来的结果当 expected。
  
F. 确定性 vs 随机性 / 执行模型
- 白盒测试,代码也是白盒;目标是 detect 环境(新代码版本,依赖版本,agent 版本)中的异常。
- 澄清:这里的「环境问题」不含网络/宿主机问题, 主要是服务版本里可能有的代码 bug 或兼容性问题。
- 目标是整体功能性测试,不是最佳效果测试;测试集本应设计得非常稳定,否则不该进测试;必要时 retry。
  - 功能性验证不需要 N 次执行。
  - 为过滤网络故障, 支持 node retry times = 2。

G. 引擎 / 执行结构
- 场景短程,很可能 in-process (Agent 接口只要给个 url,属于 env setup 的活)。
- 自建(thin Go)也 ok; 但需要支持 DAG——要收集多个 node 的执行结果后再做 judge 或下一步; DAG 不会特别大。
- CaseSpec = 薄 struct,字段即节点(无 compiler、不做 DAG 推断)。
- or dedup handler core

H. 框架抽象 / 领域无关性(关键纠偏)
- 测试框架不应感知 execute 是针对 Agent 还是普通 bash command——它们都被包装成 bash 或 http call 或独立算子。
- 整个 framework 唯一感知 LLM、唯一需要配置 LLM 的地方是 Judge。
- 工具用沙箱内真实工具,因此不太需要管对方 agent 在干什么 (黑盒算子)。

I. 工程流程 / 协作要求
- 顺序:先升 Go 最新版 → git baseline → 用 ARK API 接 judge → 让 subagent 定义 ov CLI 测试 → 跑通确认。
- 要一个 loop:① 定义者写出测试流程(代码尽量精简)→ ② executor 跑通并最终 eval 成功 → ③ 第三方(你)在卡点处决策 → ④ 说明新 case 如何复用 + ovtest 多 case 管理。
- (全局规则)可并行、上下文最小的子任务交给 sonnet/subagent 提升并行度。
- 目录:~/code/ovtest。
- 密钥:真密钥只进 .env(gitignored),example.env 还原成空模板。

J. 具体的 ov 测试场景(你定义的 expected 流程)
1. 用 火山 OV + root_api_key;
2. 用 ov admin 创建新 account:ovtest;
3. 在该 account 的 admin 用户里用 ov add-memory 记一些记忆;
4. 等待约一分钟,用 ov ls / ov find 取回;
5. 收集每阶段信息,最终交 LLM judge 评估 trace events 是否符合预期,需要先定义好一个 expected。
6. OPENVIKING_CLI_CONFIG_FILE=~/.openviking/ovcli.conf.root ov admin —— 即用 config-path 环境变量 + root config 跑 admin。

K. Stakeholders
- 测试提供方(比如 XX): 做完 feature 之后, 根据 ovtest 的 spec 生成 testing workflow & llm judge 标准
- 测试环境提供方(QA): 提供稳定的, 完成 setup 的测试环境 (火山 ov, openclaw, hermes, 开源 ov)
- 测试检测与修复(QA&RD): 发现线上测试出错或臃肿时进行测试集修复, 代码修复, 环境修复, 测试集合并和清除

        return ovtest.CaseSpec{
                ID:         "ov-memory",
                Goal:       "OpenViking stores personal facts as memories and retrieves them semantically for a fresh account.",
                Capability: "memory/store-retrieve",
                Reference: `Expected trace:
                            - "create": account "ovtest" created with admin user "ovtest-admin"; response contains a user_key.
                            - "add_mem_1..3": three personal facts added as memories, each returning ok:
                              (1) the user's name is Zayn and they maintain OpenViking;
                              (2) for systems programming the user prefers Go over Python;
                              (3) the user's favorite coffee is a flat white.
                            - "ls": lists the account's viking scopes (session/user/resources).
                            - "find": querying "What does the user prefer for systems programming?" returns at least one
                              memory whose content states the user prefers Go over Python.
                            PASS only if "find" actually surfaces the Go-over-Python preference memory.`,

                Steps: []ovtest.Step{
                        // Best-effort cleanup so the run is repeatable; no gate (account may not exist).
                        {ID: "cleanup", Node: admin("admin", "delete-account", ovAccount)},

                        {
                                ID: "create", DependsOn: []string{"cleanup"},
                                Node: admin("admin", "create-account", "--admin", ovAdminUser, ovAccount),
                                Gate: hardGate("account-created", func(br ovtest.BashResult) ovtest.CheckResult {
                                        if _, err := userKey(br.Stdout); err != nil {
                                                return ovtest.CheckResult{Pass: false, Detail: err.Error()}
                                        }
                                        return ovtest.CheckResult{Pass: true, Detail: "user_key present"}
                                }),
                        },

                        memStep("add_mem_1", data("add-memory", "My name is Zayn and I maintain the OpenViking memory system.")),
                        memStep("add_mem_2", data("add-memory", "For systems programming I prefer Go over Python.")),
                        memStep("add_mem_3", data("add-memory", "My favorite coffee drink is a flat white.")),

                        {
                                ID: "wait", DependsOn: []string{"add_mem_1", "add_mem_2", "add_mem_3"},
                                Build: data("wait", "--timeout", "120"),
                                Gate:  &ovtest.Gate{Name: "wait-ok", Policy: ovtest.GateSoft, Check: bashOK},
                        },

                        {
                                ID: "ls", DependsOn: []string{"wait"},
                                Build: data("ls"),
                                Gate:  &ovtest.Gate{Name: "ls-ok", Policy: ovtest.GateHard, Check: bashOK},
                        },

                        {
                                // Memory extraction is async and lags add-memory; each attempt sleeps
                                // `settle`s then queries, and retries until a memory surfaces.
                                ID: "find", DependsOn: []string{"wait"}, Retryable: true, Retry: 2,
                                Build: func(s *ovtest.RunState) (ovtest.Node, error) {
                                        if err := writeUserConf(s, userConf, url); err != nil {
                                                return nil, err
                                        }
                                        q := "What does the user prefer for systems programming?"
                                        return ovtest.Bash{
                                                Cmd: []string{"sh", "-c", fmt.Sprintf("sleep %s; ov find '%s' -o json", settle, q)},
                                                Env: userEnv(userConf),
                                        }, nil
                                },
                                Gate: hardGate("find-has-memory", func(br ovtest.BashResult) ovtest.CheckResult {
                                        n, err := findMemoryCount(br.Stdout)
                                        if err != nil {
                                                return ovtest.CheckResult{Pass: false, Detail: err.Error()}
                                        }
                                        if n == 0 {
                                                return ovtest.CheckResult{Pass: false, Detail: "find returned 0 memories"}
                                        }
                                        return ovtest.CheckResult{Pass: true, Detail: fmt.Sprintf("%d memories", n)}
                                }),
                        },
                },
        }