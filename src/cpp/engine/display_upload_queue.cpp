#include "display_upload_queue.h"

#include <algorithm>

void DisplayUploadQueue::set_policy(UploadQueuePolicy policy)
{
    _policy = policy;
}

size_t DisplayUploadQueue::count_state(UploadJob::State state) const
{
    size_t count = 0;
    for (const auto& job : _jobs) {
        if (job.state == state) {
            ++count;
        }
    }
    return count;
}

bool DisplayUploadQueue::has_job(int source_index, int decoder_frame) const
{
    for (const auto& job : _jobs) {
        if (job.source_index == source_index
            && job.decoder_frame == decoder_frame
            && job.state != UploadJob::State::Failed) {
            return true;
        }
    }
    return false;
}

UploadJob* DisplayUploadQueue::find_job(int source_index, int decoder_frame)
{
    for (auto& job : _jobs) {
        if (job.source_index == source_index
            && job.decoder_frame == decoder_frame
            && job.state != UploadJob::State::Failed) {
            return &job;
        }
    }
    return nullptr;
}

void DisplayUploadQueue::erase_queued_for_source(int source_index)
{
    for (auto it = _jobs.begin(); it != _jobs.end();) {
        if (it->source_index == source_index && it->state == UploadJob::State::Queued) {
            it = _jobs.erase(it);
            ++_coalesced;
        } else {
            ++it;
        }
    }
}

bool DisplayUploadQueue::drop_oldest_queued()
{
    for (auto it = _jobs.begin(); it != _jobs.end(); ++it) {
        if (it->state == UploadJob::State::Queued) {
            _jobs.erase(it);
            return true;
        }
    }
    return false;
}

bool DisplayUploadQueue::enqueue(const UploadJobRequest& req, bool already_resident)
{
    if (already_resident) {
        return false;
    }
    if (req.decoder_frame < 0) {
        return false;
    }
    if (has_job(req.source_index, req.decoder_frame)) {
        return false;
    }

    if (_policy == UploadQueuePolicy::Realtime) {
        erase_queued_for_source(req.source_index);
        while (count_state(UploadJob::State::Queued) >= kMaxPending) {
            if (!drop_oldest_queued()) {
                break;
            }
        }
    } else {
        if (count_state(UploadJob::State::Queued) >= kMaxPending) {
            ++_refused;
            return false;
        }
    }

    UploadJob job;
    job.source_index = req.source_index;
    job.decoder_frame = req.decoder_frame;
    job.upload_token = req.upload_token;
    job.kind = req.kind;
    job.width = req.width;
    job.height = req.height;
    job.channels = req.channels > 0 ? req.channels : 4;
    job.state = UploadJob::State::Queued;
    _jobs.push_back(std::move(job));
    return true;
}

bool DisplayUploadQueue::enqueue_hw(
    const UploadJobRequest& req,
    HwFrameTicket ticket,
    bool already_resident)
{
    if (!ticket.valid()) {
        return false;
    }
    if (already_resident) {
        return false;
    }
    if (req.decoder_frame < 0) {
        return false;
    }
    if (has_job(req.source_index, req.decoder_frame)) {
        return false;
    }

    if (_policy == UploadQueuePolicy::Realtime) {
        erase_queued_for_source(req.source_index);
        while (count_state(UploadJob::State::Queued) >= kMaxPending) {
            if (!drop_oldest_queued()) {
                break;
            }
        }
    } else {
        if (count_state(UploadJob::State::Queued) >= kMaxPending) {
            ++_refused;
            return false;
        }
    }

    UploadJob job;
    job.source_index = req.source_index;
    job.decoder_frame = req.decoder_frame;
    job.upload_token = req.upload_token;
    job.kind = UploadJobKind::HwImport;
    job.width = ticket.width();
    job.height = ticket.height();
    job.channels = 4;
    job.hw_ticket = std::move(ticket);
    job.state = UploadJob::State::Queued;
    _jobs.push_back(std::move(job));
    return true;
}

std::vector<UploadJob*> DisplayUploadQueue::take_queued_for_submit(size_t max_k)
{
    std::vector<UploadJob*> out;
    if (max_k == 0) {
        return out;
    }
    out.reserve(max_k);
    for (auto& job : _jobs) {
        if (job.state != UploadJob::State::Queued) {
            continue;
        }
        out.push_back(&job);
        if (out.size() >= max_k) {
            break;
        }
    }
    return out;
}

void DisplayUploadQueue::mark_uploading(UploadJob* job, uint64_t generation, size_t staging_slot)
{
    if (!job) {
        return;
    }
    job->state = UploadJob::State::Uploading;
    job->submit_generation = generation;
    job->staging_slot = staging_slot;
}

void DisplayUploadQueue::mark_failed(UploadJob* job)
{
    if (!job) {
        return;
    }
    job->texture = nullptr;
    job->hw_ticket.reset();
    job->state = UploadJob::State::Failed;
}

void DisplayUploadQueue::discard(UploadJob* job)
{
    if (!job) {
        return;
    }
    for (auto it = _jobs.begin(); it != _jobs.end(); ++it) {
        if (&(*it) == job) {
            _jobs.erase(it);
            return;
        }
    }
}

bool DisplayUploadQueue::discard_queued(int source_index, int decoder_frame)
{
    for (auto it = _jobs.begin(); it != _jobs.end(); ++it) {
        if (it->source_index == source_index
            && it->decoder_frame == decoder_frame
            && it->state == UploadJob::State::Queued) {
            _jobs.erase(it);
            return true;
        }
    }
    return false;
}

void DisplayUploadQueue::complete_generation(uint64_t completed_generation)
{
    for (auto& job : _jobs) {
        if (job.state == UploadJob::State::Uploading
            && job.submit_generation <= completed_generation) {
            job.state = UploadJob::State::Ready;
        }
    }
}

std::vector<UploadJob> DisplayUploadQueue::take_ready()
{
    std::vector<UploadJob> ready;
    for (auto it = _jobs.begin(); it != _jobs.end();) {
        if (it->state == UploadJob::State::Ready) {
            ready.push_back(std::move(*it));
            it = _jobs.erase(it);
            ++_completed;
        } else {
            ++it;
        }
    }
    return ready;
}

void DisplayUploadQueue::clear()
{
    _jobs.clear();
}

void DisplayUploadQueue::compact_failed()
{
    _jobs.erase(
        std::remove_if(
            _jobs.begin(),
            _jobs.end(),
            [](const UploadJob& job) { return job.state == UploadJob::State::Failed; }),
        _jobs.end());
}

UploadQueueStats DisplayUploadQueue::stats() const
{
    UploadQueueStats s;
    s.pending = static_cast<int>(count_state(UploadJob::State::Queued));
    s.inflight = static_cast<int>(count_state(UploadJob::State::Uploading));
    s.ready = static_cast<int>(count_state(UploadJob::State::Ready));
    s.completed = _completed;
    s.refused = _refused;
    s.coalesced = _coalesced;
    return s;
}
