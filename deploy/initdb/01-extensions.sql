-- 首次初始化时自动执行（pgvector 镜像已内置扩展二进制，但需显式启用）
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- 中文模糊匹配与相似度
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- uuid 主键

-- 中文分词建议在应用层用 jieba 预处理后写入 tsvector 列，
-- 或用 zhparser 扩展（需自行构建镜像）。
