# 🌸 Code Quality Analysis Report 🌸

## Overall Assessment

- **Quality Score**: 44.49/100
- **Quality Level**: 😷 Code reeks, mask up - Code is starting to stink, approach with caution and a mask.
- **Analyzed Files**: 36
- **Total Lines**: 6938

## Quality Metrics

| Metric | Score | Weight | Status |
|------|------|------|------|
| Comment Ratio | 12.19 | 0.15 | ✓✓ |
| Error Handling | 25.00 | 0.10 | ✓ |
| Code Structure | 30.00 | 0.15 | ✓ |
| State Management | 34.76 | 0.20 | ✓ |
| Code Duplication | 35.00 | 0.15 | ○ |
| Cyclomatic Complexity | 85.63 | 0.30 | !! |

## Problem Files (Top 5)

### 1. /build/app/vsphere/vm/vsphere_api/get_vsphere_objects.py (Score: 57.92)
**Issue Categories**: 🔄 Complexity Issues:4, 📝 Comment Issues:1, ⚠️ Other Issues:2

**Main Issues**:
- Function _mock_from_host has high cyclomatic complexity (14), consider simplifying
- Function get_vsphere_objects has very high cyclomatic complexity (17), consider refactoring
- Function '_mock_from_host' () is extremely long (164 lines), must be split
- Function '_mock_from_host' () complexity is high (14), consider simplifying
- Function 'get_vsphere_objects' () is rather long (64 lines), consider refactoring
- Function 'get_vsphere_objects' () complexity is high (17), consider simplifying
- Code comment ratio is low (8.09%), consider adding more comments

### 2. /build/app/vsphere/vm/gitlab_api/trigger_gitlab_pipeline.py (Score: 54.92)
**Issue Categories**: 📝 Comment Issues:1, ⚠️ Other Issues:1

**Main Issues**:
- Function 'trigger_gitlab_pipeline' () is rather long (59 lines), consider refactoring
- Code comment ratio is low (6.25%), consider adding more comments

### 3. /build/app/vsphere/vm/routes/main_routes.py (Score: 54.30)
**Issue Categories**: 🔄 Complexity Issues:2, ⚠️ Other Issues:1

**Main Issues**:
- Function overview_index has very high cyclomatic complexity (51), consider refactoring
- Function 'overview_index' () is extremely long (188 lines), must be split
- Function 'overview_index' () complexity is severely high (51), must be simplified

### 4. /build/app/vsphere/vm/vault/vault_manager.py (Score: 53.38)
**Issue Categories**: 📝 Comment Issues:1

**Main Issues**:
- Code comment ratio is low (7.59%), consider adding more comments

### 5. /build/app/vsphere/vm/gitlab_api/get_pipeline_status_from_gitlab.py (Score: 53.29)
**Issue Categories**: 🔄 Complexity Issues:2, ⚠️ Other Issues:2

**Main Issues**:
- Function get_pipeline_jobs has very high cyclomatic complexity (23), consider refactoring
- Function 'get_pipeline_status_from_gitlab' () is rather long (45 lines), consider refactoring
- Function 'get_pipeline_jobs' () is extremely long (242 lines), must be split
- Function 'get_pipeline_jobs' () complexity is severely high (23), must be simplified

## Improvement Suggestions

### High Priority
- Keep up the clean code standards, don't let the mess creep in

### Medium Priority
- Go further—optimize for performance and readability, just because you can
- Polish your docs and comments, make your team love you even more

