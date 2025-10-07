-- --------------------------------------------------------
-- 主機:                           172.26.1.176
-- 伺服器版本:                        8.0.43 - MySQL Community Server - GPL
-- 伺服器作業系統:                      Linux
-- HeidiSQL 版本:                  12.11.0.7065
-- --------------------------------------------------------

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8 */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;


-- 傾印 forti_db 的資料庫結構
CREATE DATABASE IF NOT EXISTS `forti_db` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci */ /*!80016 DEFAULT ENCRYPTION='N' */;
USE `forti_db`;

-- 傾印  資料表 forti_db.forti_audit_logs 結構
CREATE TABLE IF NOT EXISTS `forti_audit_logs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `actor_id` int DEFAULT NULL,
  `action` varchar(128) DEFAULT NULL,
  `entity_type` varchar(128) DEFAULT NULL,
  `entity_id` bigint DEFAULT NULL,
  `details` json DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_audit_entity` (`entity_type`,`entity_id`,`created_at`),
  KEY `idx_audit_actor` (`actor_id`,`created_at`),
  CONSTRAINT `forti_audit_logs_ibfk_1` FOREIGN KEY (`actor_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_devices 結構
CREATE TABLE IF NOT EXISTS `forti_devices` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(64) NOT NULL,
  `host` varchar(128) NOT NULL,
  `port` int NOT NULL DEFAULT '443',
  `api_token` varchar(255) DEFAULT NULL,
  `api_token_enc` varbinary(512) DEFAULT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `verify_ssl` tinyint(1) NOT NULL DEFAULT '1',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_forti_host_port` (`host`,`port`),
  UNIQUE KEY `uk_forti_device_name` (`name`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_device_vdoms 結構
CREATE TABLE IF NOT EXISTS `forti_device_vdoms` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `device_id` int NOT NULL,
  `vdom` varchar(128) NOT NULL,
  `is_active` tinyint(1) DEFAULT '1',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_device_vdom` (`device_id`,`vdom`),
  CONSTRAINT `forti_device_vdoms_ibfk_1` FOREIGN KEY (`device_id`) REFERENCES `forti_devices` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_drafts 結構
CREATE TABLE IF NOT EXISTS `forti_drafts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `title` varchar(255) NOT NULL DEFAULT '',
  `draft_action` json DEFAULT NULL,
  `check_report` json DEFAULT NULL,
  `created_by` int NOT NULL,
  `approver_id` int DEFAULT NULL,
  `status` enum('Pending_Submit','Preparing_Deploy','Awaiting_Approval','Rejected','Deploy_Succeeded','Deploy_Failed','Verify_Failed','Partial_Failed','Canceled') NOT NULL DEFAULT 'Pending_Submit',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `approved_at` datetime DEFAULT NULL,
  `executed_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `created_by` (`created_by`),
  KEY `idx_draft_dev_vdom` (`created_at`),
  KEY `idx_deleted_at` (`deleted_at`),
  KEY `idx_approver_id` (`approver_id`),
  KEY `idx_draft_status` (`status`),
  CONSTRAINT `fk_drafts_approver` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`),
  CONSTRAINT `forti_drafts_ibfk_2` FOREIGN KEY (`created_by`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_draft_approvals 結構
CREATE TABLE IF NOT EXISTS `forti_draft_approvals` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `draft_id` bigint NOT NULL,
  `approver_id` int NOT NULL,
  `decision` enum('approved','rejected') NOT NULL,
  `comment` text,
  `decided_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `approver_id` (`approver_id`),
  KEY `idx_approvals_draft_time` (`draft_id`,`decided_at`),
  KEY `idx_approvals_decision_time` (`decision`,`decided_at`),
  CONSTRAINT `fk_draft_approvals_draft` FOREIGN KEY (`draft_id`) REFERENCES `forti_drafts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `forti_draft_approvals_ibfk_2` FOREIGN KEY (`approver_id`) REFERENCES `users` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_policies_current 結構
CREATE TABLE IF NOT EXISTS `forti_policies_current` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `device_id` int NOT NULL,
  `vdom` varchar(128) NOT NULL,
  `fg_policy_id` bigint NOT NULL,
  `seq_num` int DEFAULT NULL,
  `name` varchar(255) DEFAULT NULL,
  `src_addrs` json DEFAULT NULL,
  `dst_addrs` json DEFAULT NULL,
  `services` json DEFAULT NULL,
  `src_intfs` json DEFAULT NULL,
  `dst_intfs` json DEFAULT NULL,
  `web_filter` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL,
  `action` enum('accept','deny') DEFAULT 'accept',
  `status` enum('enable','disable') DEFAULT 'enable',
  `schedule` varchar(128) DEFAULT NULL,
  `nat` tinyint(1) DEFAULT '0',
  `comments` text,
  `last_seen_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_cur` (`device_id`,`vdom`,`fg_policy_id`),
  KEY `idx_cur_list` (`device_id`,`vdom`,`action`,`status`,`seq_num`),
  KEY `idx_cur_last_seen` (`last_seen_at`),
  CONSTRAINT `forti_policies_current_ibfk_1` FOREIGN KEY (`device_id`) REFERENCES `forti_devices` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=895 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_tasks 結構
CREATE TABLE IF NOT EXISTS `forti_tasks` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `draft_id` bigint DEFAULT NULL,
  `gitlab_pipeline_id` bigint unsigned DEFAULT NULL,
  `gitlab_job_id` bigint DEFAULT NULL COMMENT 'GitLab manual/deploy job id',
  `gitlab_job_url` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL COMMENT 'GitLab job url',
  `git_commit_sha` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL COMMENT 'Pipeline commit SHA',
  `options` json DEFAULT NULL,
  `callback_token` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci DEFAULT NULL,
  `status` enum('pending','queued','running','success','failed','canceled') NOT NULL DEFAULT 'pending',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tasks_status_time` (`status`,`created_at`),
  KEY `idx_tasks_draft_id` (`draft_id`),
  KEY `idx_tasks_gitlab_pipeline` (`gitlab_pipeline_id`) USING BTREE,
  CONSTRAINT `fk_tasks_draft` FOREIGN KEY (`draft_id`) REFERENCES `forti_drafts` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=64 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.forti_task_action_results 結構
CREATE TABLE IF NOT EXISTS `forti_task_action_results` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `action_id` varchar(128) NOT NULL,
  `task_id` bigint NOT NULL,
  `kind` varchar(32) NOT NULL,
  `action_type` enum('create','update','delete') NOT NULL,
  `device_id` int NOT NULL,
  `vdom` varchar(128) NOT NULL,
  `resource_id` varchar(128) DEFAULT NULL,
  `status` enum('ok','error','skipped') NOT NULL,
  `action_order` int NOT NULL,
  `deploy_message` json DEFAULT NULL,
  `rollback` json DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `finished_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_task_action` (`task_id`,`action_id`),
  KEY `idx_task_order` (`task_id`,`action_order`),
  KEY `idx_task_status` (`task_id`,`status`),
  KEY `idx_task_action_latest` (`task_id`,`action_id`,`finished_at`,`id`),
  KEY `fk_tar_device_vdom` (`device_id`,`vdom`),
  KEY `idx_tar_task_action` (`task_id`,`action_id`),
  CONSTRAINT `fk_tar_device` FOREIGN KEY (`device_id`) REFERENCES `forti_devices` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_tar_device_vdom` FOREIGN KEY (`device_id`, `vdom`) REFERENCES `forti_device_vdoms` (`device_id`, `vdom`) ON DELETE RESTRICT,
  CONSTRAINT `fk_tar_task` FOREIGN KEY (`task_id`) REFERENCES `forti_tasks` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.gitlab_info 結構
CREATE TABLE IF NOT EXISTS `gitlab_info` (
  `id` int NOT NULL AUTO_INCREMENT,
  `url` text NOT NULL,
  `token` text NOT NULL,
  `project_id` int NOT NULL DEFAULT '0',
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.operation_logs 結構
CREATE TABLE IF NOT EXISTS `operation_logs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(100) DEFAULT NULL,
  `action` varchar(100) DEFAULT NULL,
  `detail` text,
  `timestamp` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.permissions 結構
CREATE TABLE IF NOT EXISTS `permissions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `permission_key` varchar(100) NOT NULL,
  `category` varchar(100) DEFAULT NULL,
  `created_at` datetime DEFAULT (now()),
  `updated_at` datetime DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `permission_key` (`permission_key`)
) ENGINE=InnoDB AUTO_INCREMENT=15 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.roles 結構
CREATE TABLE IF NOT EXISTS `roles` (
  `id` int NOT NULL AUTO_INCREMENT,
  `role_name` varchar(50) NOT NULL,
  `created_at` datetime DEFAULT (now()),
  `updated_at` datetime DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=6 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.role_permissions 結構
CREATE TABLE IF NOT EXISTS `role_permissions` (
  `role_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`role_id`,`permission_id`),
  KEY `permission_id` (`permission_id`),
  CONSTRAINT `role_permissions_ibfk_1` FOREIGN KEY (`role_id`) REFERENCES `roles` (`id`) ON DELETE CASCADE,
  CONSTRAINT `role_permissions_ibfk_2` FOREIGN KEY (`permission_id`) REFERENCES `permissions` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.sidebar_items 結構
CREATE TABLE IF NOT EXISTS `sidebar_items` (
  `id` int NOT NULL AUTO_INCREMENT,
  `section_id` int DEFAULT NULL,
  `name` varchar(100) DEFAULT NULL,
  `permission_key` varchar(50) DEFAULT NULL,
  `endpoint` varchar(100) DEFAULT NULL,
  `sort_order` int NOT NULL DEFAULT '0',
  `created_at` datetime DEFAULT (now()),
  `updated_at` datetime DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `section_id` (`section_id`),
  KEY `fk_sidebar_permission` (`permission_key`),
  CONSTRAINT `fk_sidebar_permission` FOREIGN KEY (`permission_key`) REFERENCES `permissions` (`permission_key`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `sidebar_items_ibfk_1` FOREIGN KEY (`section_id`) REFERENCES `sidebar_sections` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=13 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.sidebar_sections 結構
CREATE TABLE IF NOT EXISTS `sidebar_sections` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(50) DEFAULT NULL,
  `icon_class` varchar(50) DEFAULT NULL,
  `identifier` varchar(50) DEFAULT NULL,
  `sort_order` int NOT NULL DEFAULT '0',
  `created_at` datetime DEFAULT (now()),
  `updated_at` datetime DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.users 結構
CREATE TABLE IF NOT EXISTS `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(50) NOT NULL,
  `password_hash` varchar(255) DEFAULT NULL,
  `auth_source` enum('local','ad') NOT NULL DEFAULT 'local',
  `created_at` datetime DEFAULT (now()),
  `updated_at` datetime DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_username` (`username`),
  UNIQUE KEY `id` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=5 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

-- 傾印  資料表 forti_db.user_roles 結構
CREATE TABLE IF NOT EXISTS `user_roles` (
  `id` int NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `role_id` int NOT NULL,
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE KEY `uniq_user_role` (`user_id`,`role_id`) USING BTREE,
  UNIQUE KEY `uq_user_role` (`user_id`,`role_id`) USING BTREE,
  KEY `idx_user_roles_user` (`user_id`) USING BTREE,
  KEY `idx_user_roles_role` (`role_id`) USING BTREE,
  CONSTRAINT `fk_role` FOREIGN KEY (`role_id`) REFERENCES `roles` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_user_roles_role` FOREIGN KEY (`role_id`) REFERENCES `roles` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_user_roles_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB AUTO_INCREMENT=22 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 取消選取資料匯出。

/*!40103 SET TIME_ZONE=IFNULL(@OLD_TIME_ZONE, 'system') */;
/*!40101 SET SQL_MODE=IFNULL(@OLD_SQL_MODE, '') */;
/*!40014 SET FOREIGN_KEY_CHECKS=IFNULL(@OLD_FOREIGN_KEY_CHECKS, 1) */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40111 SET SQL_NOTES=IFNULL(@OLD_SQL_NOTES, 1) */;
