// SPDX-License-Identifier: GPL-3.0-only
// Native x64 helper ABI consumed by collector.py. PDH uses English counter
// names on every Windows UI language. Call NcmSample serially, prime once,
// wait at least a second, then sample deltas. Close with NcmClose.
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <winioctl.h>
#include <pdh.h>
#include <pdhmsg.h>
#include <dxgi.h>
#include <algorithm>
#include <cmath>
#include <map>
#include <string>
#include <vector>

// Percentages are doubles in [0,100]; -1 means unsupported/invalid this sample.
// effective is a logical-processor count; dedicatedUsed/Total are byte counts.
// Adapter-wide memory counters avoid counting shared allocations twice.
struct HardwareSample {
    double cpu, effective, ram, disk, gpu, vram;
    unsigned logicalProcessors;
    unsigned long long dedicatedUsed, dedicatedTotal;
};
struct CounterItem { std::wstring name; double value; };
static PDH_HQUERY query{};
static PDH_HCOUNTER cpuCounter{}, diskCounter{}, gpuCounter{}, memoryCounter{};
static std::vector<unsigned> systemDisks;
static std::wstring adapterLuid;
static unsigned long long dedicatedTotal;
static std::wstring Lower(std::wstring value) {
    std::transform(value.begin(),value.end(),value.begin(),towlower);return value;
}

static std::vector<CounterItem> Values(PDH_HCOUNTER counter) {
    std::vector<CounterItem> result;
    if (!counter) return result;
    DWORD bytes=0, count=0;
    if (PdhGetFormattedCounterArrayW(counter,PDH_FMT_DOUBLE|PDH_FMT_NOCAP100,&bytes,&count,nullptr)!=PDH_MORE_DATA) return result;
    std::vector<unsigned char> buffer(bytes);
    auto* items=reinterpret_cast<PDH_FMT_COUNTERVALUE_ITEM_W*>(buffer.data());
    if (PdhGetFormattedCounterArrayW(counter,PDH_FMT_DOUBLE|PDH_FMT_NOCAP100,&bytes,&count,items)!=ERROR_SUCCESS) return result;
    for (DWORD i=0;i<count;i++) {
        auto status=items[i].FmtValue.CStatus;
        if ((status==PDH_CSTATUS_VALID_DATA || status==PDH_CSTATUS_NEW_DATA) && std::isfinite(items[i].FmtValue.doubleValue))
            result.push_back({items[i].szName,items[i].FmtValue.doubleValue});
    }
    return result;
}
static bool Init() {
    // Map C: volume extents to physical disks. Their active time includes all
    // partitions on those disks; Windows exposes no per-volume active-time PDH
    // counter. For a multi-disk C: volume, report its busiest backing disk.
    if (PdhOpenQueryW(nullptr,0,&query)!=ERROR_SUCCESS) return false;
    PdhAddEnglishCounterW(query,L"\\Processor Information(*)\\% Processor Time",0,&cpuCounter);
    PdhAddEnglishCounterW(query,L"\\PhysicalDisk(*)\\% Idle Time",0,&diskCounter);
    PdhAddEnglishCounterW(query,L"\\GPU Engine(*)\\Utilization Percentage",0,&gpuCounter);
    PdhAddEnglishCounterW(query,L"\\GPU Adapter Memory(*)\\Dedicated Usage",0,&memoryCounter);
    auto volume=CreateFileW(L"\\\\.\\C:",0,FILE_SHARE_READ|FILE_SHARE_WRITE,nullptr,OPEN_EXISTING,0,nullptr);
    if (volume!=INVALID_HANDLE_VALUE) {
        unsigned char buffer[4096]{}; DWORD returned=0;
        if (DeviceIoControl(volume,IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,nullptr,0,buffer,sizeof(buffer),&returned,nullptr)) {
            auto* extents=reinterpret_cast<VOLUME_DISK_EXTENTS*>(buffer);
            for (DWORD i=0;i<extents->NumberOfDiskExtents;i++) systemDisks.push_back(extents->Extents[i].DiskNumber);
        }
        CloseHandle(volume);
    }
    IDXGIFactory1* factory=nullptr;
    // Select the first hardware DXGI adapter. This is one adapter, not an
    // aggregate across GPUs; only its dedicated capacity is used for VRAM.
    if (SUCCEEDED(CreateDXGIFactory1(__uuidof(IDXGIFactory1),reinterpret_cast<void**>(&factory)))) {
        for (UINT index=0;;index++) {
            IDXGIAdapter1* adapter=nullptr;
            if (factory->EnumAdapters1(index,&adapter)==DXGI_ERROR_NOT_FOUND) break;
            if (!adapter) break;
            DXGI_ADAPTER_DESC1 desc{};adapter->GetDesc1(&desc);adapter->Release();
            if (desc.Flags&DXGI_ADAPTER_FLAG_SOFTWARE) continue;
            wchar_t luid[80];swprintf(luid,80,L"luid_0x%08x_0x%08x",static_cast<unsigned>(desc.AdapterLuid.HighPart),desc.AdapterLuid.LowPart);
            adapterLuid=luid;dedicatedTotal=desc.DedicatedVideoMemory;break;
        }
        factory->Release();
    }
    return true;
}
extern "C" __declspec(dllexport) int NcmSample(HardwareSample* out) {
    if (!out) return 0;
    *out={-1,-1,-1,-1,-1,-1,0,0,dedicatedTotal};
    bool ready=query || Init();
    out->dedicatedTotal=dedicatedTotal;
    MEMORYSTATUSEX memory{sizeof(memory)};
    if (GlobalMemoryStatusEx(&memory) && memory.ullTotalPhys)
        out->ram=100.0*(memory.ullTotalPhys-memory.ullAvailPhys)/memory.ullTotalPhys;
    if (!ready || PdhCollectQueryData(query)!=ERROR_SUCCESS) return 0;
    double sum=0,squares=0;unsigned observed=0;
    out->logicalProcessors=GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    for (const auto& item:Values(cpuCounter)) {
        if (item.name.find(L"_Total")!=std::wstring::npos) continue;
        double u=std::clamp(item.value,0.0,100.0);sum+=u;squares+=u*u;observed++;
    }
    if (out->logicalProcessors && observed==out->logicalProcessors) {
        // Require every logical processor from the same interval. Aggregate
        // _Total instances are excluded. C_eff = (sum u)^2 / sum(u^2), with
        // exactly idle reported as 0. Percent vs fraction units cancel.
        out->cpu=sum/out->logicalProcessors;
        out->effective=squares>1e-12 ? sum*sum/squares : 0;
    }
    for (const auto& item:Values(diskCounter)) {
        if (item.name==L"_Total") continue;
        wchar_t* end=nullptr;unsigned disk=wcstoul(item.name.c_str(),&end,10);
        if (end!=item.name.c_str() && std::find(systemDisks.begin(),systemDisks.end(),disk)!=systemDisks.end())
            out->disk=std::max(out->disk,std::clamp(100.0-item.value,0.0,100.0));
    }
    std::map<std::wstring,double> engines;
    // PDH exposes per-process engine counters. Sum the same physical engine
    // across processes, then report the busiest engine rather than their sum.
    for (const auto& item:Values(gpuCounter)) {
        if (adapterLuid.empty() || Lower(item.name).find(adapterLuid)==std::wstring::npos) continue;
        auto engine=item.name.find(L"_phys_");
        if (engine!=std::wstring::npos) engines[item.name.substr(engine)]+=std::max(0.0,item.value);
    }
    for (const auto& [engine,value]:engines) out->gpu=std::max(out->gpu,std::clamp(value,0.0,100.0));
    for (const auto& item:Values(memoryCounter)) {
        if (!adapterLuid.empty() && Lower(item.name).find(adapterLuid)!=std::wstring::npos) {
            out->dedicatedUsed+=static_cast<unsigned long long>(std::max(0.0,item.value));
            if (out->dedicatedTotal) out->vram=100.0*out->dedicatedUsed/out->dedicatedTotal;
        }
    }
    return 1;
}
extern "C" __declspec(dllexport) void NcmClose() {
    if (query) PdhCloseQuery(query);
    query=nullptr;cpuCounter=diskCounter=gpuCounter=memoryCounter=nullptr;
    systemDisks.clear();adapterLuid.clear();dedicatedTotal=0;
}
extern "C" __declspec(dllexport) const wchar_t* NcmDiagnostic() {
    static std::wstring result;
    result=adapterLuid+L"; cpu="+std::to_wstring(Values(cpuCounter).size())+L" gpu="+std::to_wstring(Values(gpuCounter).size())+L" memory="+std::to_wstring(Values(memoryCounter).size());
    return result.c_str();
}
