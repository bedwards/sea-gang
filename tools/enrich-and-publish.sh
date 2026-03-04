#!/usr/bin/env bash
# Auto-publish enrichment runner
# Runs sea-gang jobs one at a time, regenerates static site,
# commits and pushes after each enrichment.
set -euo pipefail

cd /Users/bedwards/vibe/sea-gang
source .venv/bin/activate

HEX_DIR="/Users/bedwards/hex-index"
COUNT=0
MAX_JOBS=12

echo "🌊 Starting auto-publish enrichment runner"

for i in $(seq 1 $MAX_JOBS); do
    # Check for pending jobs
    QUEUE_OUT=$(sea-gang queue 2>/dev/null)
    if echo "$QUEUE_OUT" | grep -q "empty"; then
        echo ""
        echo "✅ Queue empty after $COUNT enrichments. Done!"
        break
    fi
    
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "🔄 Enrichment #$((COUNT+1)) of $MAX_JOBS — $(date '+%H:%M:%S')"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    # Run one job
    START_TIME=$(date +%s)
    sea-gang run --once --no-schedule 2>&1
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    
    # Get last job info from log directory
    LATEST_LOG=$(ls -t /Users/bedwards/.config/sea-gang/logs/hex-index/wikipedia_enrich_* /Users/bedwards/.config/sea-gang/logs/hex-index/enrich_latest_* 2>/dev/null | head -1)
    
    if [ -n "$LATEST_LOG" ]; then
        COUNT=$((COUNT+1))
        echo "⏱️  Enrichment took ${DURATION}s"
        echo ""
        echo "📦 Regenerating static site..."
        cd "$HEX_DIR"
        npm run static:generate 2>&1 | grep -E "(Summary|pages)" | tail -6
        
        echo ""
        echo "📤 Committing and pushing..."
        git add docs/
        COMMIT_MSG="feat: Wikipedia deep-dive #$COUNT (sea-gang auto-publish)"
        if git diff --cached --quiet 2>/dev/null; then
            echo "   No changes to commit (article may not have generated new content)"
        else
            git commit -m "$COMMIT_MSG" --no-verify 2>&1 | tail -2
            git push 2>&1 | tail -3
            echo "✅ Published enrichment #$COUNT"
        fi
        
        cd /Users/bedwards/vibe/sea-gang
    else
        echo "⚠️  No enrichment log found (job may have failed)"
    fi
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Final stats after $COUNT enrichments:"
sea-gang stats
sea-gang history -n "$MAX_JOBS"
