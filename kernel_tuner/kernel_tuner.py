#!/usr/bin/env python
""" A simple CUDA kernel tuner in Python

The goal of this project is to provide a - as simple as possible - tool
for tuning CUDA kernels. This implies that any CUDA kernel can be tuned
without requiring extensive changes to the original kernel code.

A very common problem in CUDA programming is that some combination of
thread block dimensions and other kernel parameters, like tiling or
unrolling factors, results in dramatically better performance than other
kernel configurations. The goal of auto-tuning is to automate the
process of finding the best performing configuration for a given device.

This kernel tuner aims that you can directly use the tuned kernels
without introducing any new dependencies. The tuned kernels can
afterwards be used independently of the programming environment, whether
that is using C/C++/Java/Fortran or Python doesn't matter.

This module currently only contains one function which is called
tune_kernel() to which you pass at least the kernel name, a string
containing the kernel code, the problem size, a list of kernel function
arguments, and a dictionary of tunable parameters. There are also a lot
of optional parameters, for a full list see the documentation of
tune_kernel().
"""

"""
Example usage
-------------
See the bottom of this file (kernel_tuner.py)

Author
------
Ben van Werkhoven <b.vanwerkhoven@esciencenter.nl>

Copyright and License
---------------------
* Copyright 2016 Netherlands eScience Center

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import numpy
import itertools

#embedded in try block to be able to generate documentation
#and run many of the tests on machines with no CUDA capable GPU
try:
    import pycuda.driver as drv
    from pycuda.autoinit import context
    from pycuda.compiler import SourceModule
except Exception:
    pass

def tune_kernel(kernel_name, kernel_string, problem_size, arguments,
        tune_params, device=0, grid_div_x=None, grid_div_y=None,
        restrictions=None, verbose=False):
    """ Tune a CUDA kernel given a set of tunable parameters

    :param kernel_name: The name of the kernel in the code
    :type kernel_name: string

    :param kernel_string: The CUDA kernel code as a string
    :type kernel_string: string

    :param problem_size: A tuple containing the size from which the grid
            dimensions of the kernel will be computed. Do not divide by
            the thread block sizes, if this is necessary use grid_div_x/y to
            specify.
    :type problem_size: tuple(int, int)

    :param arguments: A list of kernel arguments, use numpy arrays for
            arrays, use numpy.int32 or numpy.float32 for singulars
    :type arguments: list

    :param tune_params: A dictionary containing the parameter names as keys
            and lists of possible parameter settings as values.
            The kernel tuner will try to compile and benchmark all possible
            combinations of all possible values for all tuning parameters.
            This typically results in a rather large search space of all
            possible kernel configurations.
            For each kernel configuration, each tuning parameter is
            replaced at compile-time with its current value.
            Currently the kernel tuner uses the convention that the following
            list of tuning parameters are used as thread block dimensions:

                * "block_size_x"   thread block size x-dimension
                * "block_size_y"   thread block size y-dimension
                * "block_size_z"   thread block size z-dimension

            Options for changing these defaults will be added later. If you
            don't want the thread block dimensions to be compiled in, you
            may use the built-in variables blockDim.xyz instead

    :type tune_params: dict( string : [int, int, ...] )

    :param device: CUDA device to use, in case you have multiple CUDA-capable
        GPUs you may use this to select one, 0 by default.
    :type device: int

    :param grid_div_x: A list of names of the parameters whose values divide
        the grid dimensions in the x-direction. If not supplied,
        ["block_size_x"] will be used by default, if do not want this pass
        an empty list.
    :type grid_div_x: list

    :param grid_div_y: A list of names of the parameters whose values divide
        the grid dimensions in the y-direction, None by default
    :type grid_div_y: list

    :param restrictions: A list of strings containing boolean expression that
        limited the search space in that they must be satisfied by the kernel
        configuration. These expressions must be true for the configuration
        to be part of the search space. For example:
        restrictions=["block_size_x==block_size_y*tile_size_y"] limits the
        search to configurations where the block_size_x equals the product
        of block_size_y and tile_size_y.
        The default is None.
    :type restrictions: list

    :param verbose: Sets whether or not to report about configurations that
        were skipped during the search. This could be due to several reasons:

            * kernel configuration fails one or more restrictions
            * too many threads per thread block
            * too much shared memory used by the kernel
            * too many resources requested for launch

        verbose is set to False by default.
    :type verbose: boolean

    :returns: A dictionary of all executed kernel configurations and their
        execution times.
    :rtype: dict( string, float )
    """

    original_kernel = kernel_string
    results = dict()

    #inspect device properties
    devprops = { str(k): v for (k, v) in drv.Device(device).get_attributes().items() }
    max_threads = devprops['MAX_THREADS_PER_BLOCK']
    cc = str(devprops['COMPUTE_CAPABILITY_MAJOR']) + str(devprops['COMPUTE_CAPABILITY_MINOR'])

    #move data to GPU
    gpu_args = _create_gpu_args(arguments)

    #compute cartesian product of all tunable parameters
    for element in itertools.product(*tune_params.values()):
        params = dict(zip(tune_params.keys(), element))
        instance_string = "_".join([str(i) for i in params.values()])

        #check for search space restrictions
        try:
            _check_restrictions(restrictions, params)
        except Exception, e:
            if verbose:
                print "skipping config", instance_string, "reason:", str(e)
            continue

        #compute thread block and grid dimensions for this kernel
        threads = _get_thread_block_dimensions(params)
        if numpy.prod(threads) > max_threads:
            if verbose:
                print "skipping config", instance_string, "reason: too many threads per block"
            continue
        grid = _get_grid_dimensions(problem_size, params,
                       grid_div_y, grid_div_x)

        #create configuration specific kernel string
        kernel_string = _prepare_kernel_string(original_kernel, params)

        #rename the kernel to guarantee that PyCuda compiles a new kernel
        name = kernel_name + "_" + instance_string
        kernel_string = kernel_string.replace(kernel_name, name)

        #compile kernel func
        try:
            func = SourceModule(kernel_string, options=['-Xcompiler=-Wall'],
                    arch='compute_' + cc, code='sm_' + cc,
                    cache_dir=False).get_function(name)
        except drv.CompileError, e:
            #compiles may fail because certain kernel configurations use too
            #much shared memory for example, the desired behavior is to simply
            #skip over this configuration and try the next one
            if "uses too much shared data" in e.stderr:
                if verbose:
                    print "skipping config", instance_string, "reason: too much shared memory used"
                continue
            else:
                raise e

        #test kernel
        try:
            time = _benchmark(func, gpu_args, threads, grid)
        except drv.LaunchError, e:
            #some launches may fail because too many registers are required
            #to run the kernel given the current thread block size
            #the desired behavior is to simply skip over this configuration
            #and proceed to try the next one
            if "too many resources requested for launch" in str(e):
                if verbose:
                    print "skipping config", instance_string, "reason: too many resources requested for launch"
                continue
            else:
                raise e
        except Exception, e:
            print "Error while benchmarking:", instance_string
            raise e


        #print the result
        print params, kernel_name, "took:", time, " ms."
        results[instance_string] = time

    #finished iterating over search space
    best_config = min(results, key=results.get)
    print "best performing configuration: ", best_config, "took:", results[best_config], "ms."

    return results


#module private functions

def _create_gpu_args(arguments):
    """ready argument list to be passed to the kernel, allocates gpu mem"""
    gpu_args = []
    for arg in arguments:
        # if arg i is a numpy array copy to device
        if isinstance(arg, numpy.ndarray):
            gpu_args.append(drv.mem_alloc(arg.nbytes))
            drv.memcpy_htod(gpu_args[-1], arg)
        else: # if not an array, just pass argument along
            gpu_args.append(arg)
    return gpu_args

def _get_grid_dimensions(problem_size, params, grid_div_y, grid_div_x):
    """compute grid dims based on problem sizes and listed grid divisors"""
    div_x = 1
    if grid_div_x is None and "block_size_x" in params:
        grid_div_x = ["block_size_x"]
    if grid_div_x is not None:
        div_x = numpy.prod([params[i] for i in grid_div_x])
    div_y = 1
    if grid_div_y is not None:
        div_y = numpy.prod([params[i] for i in grid_div_y])
    grid = (int(numpy.ceil(float(problem_size[0]) / float(div_x))),
            int(numpy.ceil(float(problem_size[1]) / float(div_y))) )
    return grid

def _get_thread_block_dimensions(params):
    """thread block size from tuning params, currently using convention"""
    block_size_x = params.get("block_size_x", 256)
    block_size_y = params.get("block_size_y", 1)
    block_size_z = params.get("block_size_z", 1)
    return (block_size_x, block_size_y, block_size_z)

def _prepare_kernel_string(original_kernel, params):
    """replace occurrences of the tuning params with their current value"""
    kernel_string = original_kernel
    for k, v in params.iteritems():
        kernel_string = kernel_string.replace(k, str(v))
    return kernel_string

def _benchmark(func, gpu_args, threads, grid):
    """runs the kernel and measures time repeatedly, returns average time"""
    ITERATIONS = 7
    start = drv.Event()
    end = drv.Event()
    times = []
    for i in range(ITERATIONS):
        context.synchronize()
        start.record()
        func(*gpu_args, block=threads, grid=grid)
        end.record()
        context.synchronize()
        times.append(end.time_since(start))
    times = sorted(times)
    return numpy.mean(times[1:-1])

def _check_restrictions(restrictions, params):
    if restrictions != None:
        for restrict in restrictions:
            if not eval(_prepare_kernel_string(restrict, params)):
                raise Exception("config fails restriction")





if __name__ == "__main__":
    #The following shows a simple example use of the kernel tuner

    kernel_string = """
    __global__ void vector_add(float *c, float *a, float *b, int n) {
        int i = blockIdx.x * block_size_x + threadIdx.x;
        if (i<n) {
            c[i] = a[i] + b[i];
        }
    }
    """

    size = 10000000
    problem_size = (size, 1)

    a = numpy.random.randn(size).astype(numpy.float32)
    b = numpy.random.randn(size).astype(numpy.float32)
    c = numpy.zeros_like(b)

    args = [c, a, b]

    tune_params = dict()
    tune_params["block_size_x"] = [128+64*i for i in range(15)]

    tune_kernel("vector_add", kernel_string, problem_size, args, tune_params)


