-- 合同审查系统初始化 Schema（规范 §2.4.9 八张表）
CREATE DATABASE IF NOT EXISTS contract_review DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE contract_review;

-- 审批任务（唯一业务标识去重）
CREATE TABLE IF NOT EXISTS approval_tasks (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    approval_code  VARCHAR(64) NOT NULL COMMENT '唯一业务标识',
    approval_title VARCHAR(255) NOT NULL,
    applicant_name VARCHAR(64) NOT NULL DEFAULT '',
    instance_id    VARCHAR(64) NOT NULL COMMENT 'mock审批系统实例ID',
    task_status    VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending|parsing|reviewing|blocked|done',
    write_status   VARCHAR(16) NOT NULL DEFAULT 'not_written' COMMENT 'not_written|writing|success|failed',
    block_reason   VARCHAR(512) NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_approval_code (approval_code),
    KEY idx_tasks_status (task_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 附件
CREATE TABLE IF NOT EXISTS approval_attachments (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id         BIGINT NOT NULL COMMENT '逻辑FK approval_tasks.id',
    attachment_id   VARCHAR(64) NOT NULL,
    file_name       VARCHAR(255) NOT NULL,
    file_type       VARCHAR(16) NOT NULL DEFAULT '',
    file_path       VARCHAR(512) NOT NULL,
    download_status VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending|done|failed',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_attach (task_id, attachment_id),
    KEY idx_attach_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 合同解析结果
CREATE TABLE IF NOT EXISTS contract_parses (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id          BIGINT NOT NULL COMMENT '逻辑FK approval_tasks.id',
    basic_info_json  JSON NULL COMMENT '标题/编号/主体/对方/金额/币种/生效/到期',
    clause_info_json JSON NULL COMMENT '八类条款: {name: {text,pos,status}}',
    parse_status     VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'pending|done|failed',
    parse_error      VARCHAR(512) NULL,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_parse_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 审查规则库
CREATE TABLE IF NOT EXISTS review_rules (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    rule_code       VARCHAR(64) NOT NULL,
    rule_name       VARCHAR(128) NOT NULL,
    risk_level      VARCHAR(8) NOT NULL COMMENT 'high|medium|low',
    rule_status     TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
    match_mode      VARCHAR(16) NOT NULL COMMENT 'keyword|regex|absence',
    match_text      TEXT NOT NULL COMMENT '关键词/正则/缺失探测关键词组(逗号分隔)',
    suggestion_text VARCHAR(512) NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '工程超集字段(偏差登记)',
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_rule_code (rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 规则命中
CREATE TABLE IF NOT EXISTS rule_hits (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id           BIGINT NOT NULL COMMENT '逻辑FK approval_tasks.id',
    rule_id           BIGINT NOT NULL COMMENT '逻辑FK review_rules.id',
    evidence_text     VARCHAR(1024) NOT NULL COMMENT '命中的原文片段',
    evidence_position VARCHAR(64) NOT NULL DEFAULT '' COMMENT '字符偏移或条款位置',
    hit_status        VARCHAR(16) NOT NULL DEFAULT 'hit' COMMENT 'hit|miss|error',
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_hits_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 审查结果
CREATE TABLE IF NOT EXISTS review_results (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id             BIGINT NOT NULL COMMENT '逻辑FK approval_tasks.id',
    overall_risk_level  VARCHAR(8) NOT NULL COMMENT 'high|medium|low',
    summary_text        TEXT NOT NULL,
    focus_points_json   JSON NULL,
    comment_text        MEDIUMTEXT NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_results_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 评论回写日志
CREATE TABLE IF NOT EXISTS comment_logs (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id             BIGINT NOT NULL,
    write_status        VARCHAR(16) NOT NULL COMMENT 'writing|success|failed',
    write_response_text VARCHAR(512) NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_clog_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 全链路日志
CREATE TABLE IF NOT EXISTS task_logs (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id     BIGINT NULL,
    log_level   VARCHAR(8) NOT NULL DEFAULT 'info' COMMENT 'info|warn|error',
    log_type    VARCHAR(32) NOT NULL DEFAULT '' COMMENT 'fetch|download|parse|rule|write|agent',
    log_content VARCHAR(1024) NOT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_tlogs_task (task_id),
    KEY idx_tlogs_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent 运行记录（第九表·工程超集·偏差已登记：断点恢复/预算审计/降级溯源）
CREATE TABLE IF NOT EXISTS agent_runs (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id           BIGINT NOT NULL,
    channel           VARCHAR(16) NOT NULL DEFAULT 'pending' COMMENT 'native|json|deterministic|pending',
    status            VARCHAR(16) NOT NULL DEFAULT 'running' COMMENT 'running|succeeded|blocked|failed',
    dry_run           TINYINT NOT NULL DEFAULT 0,
    steps_used        INT NOT NULL DEFAULT 0,
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    llm_calls         INT NOT NULL DEFAULT 0,
    wall_ms           INT NOT NULL DEFAULT 0,
    fallback_kind     VARCHAR(32) NULL COMMENT 'budget_steps|budget_tokens|budget_wall|circuit_open|llm_down|model_no_write',
    prompt_version    VARCHAR(32) NOT NULL DEFAULT '',
    model_name        VARCHAR(64) NOT NULL DEFAULT '',
    messages_json     JSON NULL COMMENT '最近消息快照(resume源)',
    error_digest      VARCHAR(512) NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    finished_at       DATETIME NULL,
    KEY idx_runs_task (task_id),
    KEY idx_runs_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
