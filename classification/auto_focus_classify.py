
import numpy as np
from tompy.mpi import MPI
from pytom.basic.structures import ParticleList

assert np.__version__ >= '1.7.0'

mpi = MPI()


def combinations(iterable, r):
    # combinations('ABCD', 2) --> AB AC AD BC BD CD
    # combinations(range(4), 3) --> 012 013 023 123
    pool = tuple(iterable)
    n = len(pool)
    if r > n:
        return
    indices = range(r)
    yield tuple(pool[i] for i in indices)
    while True:
        for i in reversed(range(r)):
            if indices[i] != i + n - r:
                break
        else:
            return
        indices[i] += 1
        for j in range(i+1, r):
            indices[j] = indices[j-1] + 1
        yield tuple(pool[i] for i in indices)


def calculate_difference_map(v1, band1, v2, band2, mask=None, focus_mask=None, align=True, sigma=None, threshold=0.4):
    """mask if for alignment, while focus_mask is for difference map.
    """
    from pytom_volume import vol, power, abs, limit, transformSpline, variance, mean, max, min
    from pytom.basic.normalise import mean0std1
    from pytom.basic.filter import lowpassFilter

    # do lowpass filtering first
    lv1 = lowpassFilter(v1, band1, band1/10.)[0]
    lv2 = lowpassFilter(v2, band2, band2/10.)[0]

    # do alignment of two volumes, if required. v1 is used as reference.
    if align:
        from sh_alignment.frm import frm_align
        band = int(band1 if band1<band2 else band2)
        pos, angle, score = frm_align(lv2, None, lv1, None, [4,64], band, lv1.sizeX()/4, mask)
        shift = [pos[0]-v1.sizeX()/2, pos[1]-v1.sizeY()/2, pos[2]-v1.sizeZ()/2]

        # transform v2
        lvv2 = vol(lv2)
        transformSpline(lv2, lvv2, -angle[1],-angle[0],-angle[2],lv2.sizeX()/2,lv2.sizeY()/2,lv2.sizeZ()/2,-shift[0],-shift[1],-shift[2],0,0,0)
    else:
        lvv2 = lv2

    # do normalization
    mean0std1(lv1)
    mean0std1(lvv2)

    # only consider the density beyond certain sigma
    if sigma is None or sigma == 0:
        pass
    elif sigma < 0: # negative density counts
        assert min(lv1) < sigma
        assert min(lvv2) < sigma
        limit(lv1, 0,0, sigma,0, False, True)
        limit(lvv2, 0,0, sigma,0, False, True)
    else: # positive density counts
        assert max(lv1) > sigma
        assert max(lvv2) > sigma
        limit(lv1, sigma,0, 0,0, True, False)
        limit(lvv2, sigma,0, 0,0, True, False)

    # if we want to focus on specific area only
    if focus_mask:
        lv1 *= focus_mask
        lvv2 *= focus_mask

    # calculate the STD map
    avg = (lv1+lvv2)/2
    var1 = avg - lv1
    power(var1, 2)
    var2 = avg - lvv2
    power(var2, 2)

    std_map = var1+var2
    power(std_map, 0.5)

    # calculate the coefficient of variance map
    # std_map = std_map/abs(avg)

    if focus_mask:
        std_map *= focus_mask

    # threshold the STD map
    mv = mean(std_map)
    threshold = mv+(max(std_map)-mv)*threshold
    limit(std_map, threshold, 0, threshold, 1, True, True)

    # do a lowpass filtering
    std_map1 = lowpassFilter(std_map, v1.sizeX()/4, v1.sizeX()/40.)[0]

    if align:
        std_map2 = vol(std_map)
        transformSpline(std_map1, std_map2, angle[0],angle[1],angle[2],v1.sizeX()/2,v1.sizeY()/2,v1.sizeZ()/2,0,0,0,shift[0],shift[1],shift[2])
    else:
        std_map2 = std_map1

    limit(std_map1, 0.5, 0, 1, 1, True, True)
    limit(std_map2, 0.5, 0, 1, 1, True, True)
    
    # return the respective difference maps
    return (std_map1, std_map2)


def calculate_difference_map_proxy(r1, band1, r2, band2, mask, focus_mask, binning, iteration, sigma, threshold):
    from pytom_volume import read
    from pytom.basic.structures import Particle
    v1 = r1.getVolume()
    v2 = r2.getVolume()
    if mask:
        mask = read(mask, 0,0,0,0,0,0,0,0,0, binning,binning,binning)
    else:
        mask = None
    if focus_mask:
        focus_mask = read(focus_mask, 0,0,0,0,0,0,0,0,0, binning,binning,binning)
    else:
        focus_mask = None

    (dmap1, dmap2) = calculate_difference_map(v1, band1, v2, band2, mask, focus_mask, True, sigma, threshold)
    fname1 = 'iter'+str(iteration)+'_dmap_'+str(r1.getClass())+'_'+str(r2.getClass())+'.em'
    dmap1.write(fname1)
    fname2 = 'iter'+str(iteration)+'_dmap_'+str(r2.getClass())+'_'+str(r1.getClass())+'.em'
    dmap2.write(fname2)

    dp1 = Particle(fname1)
    dp1.setClass(r1.getClass())
    dp2 = Particle(fname2)
    dp2.setClass(r2.getClass())

    return (dp1, dp2)


def focus_score(p, ref, freq, diff_mask, binning):
    from pytom.basic.correlation import nxcc
    from pytom.basic.filter import lowpassFilter
    v = p.getTransformedVolume(binning)
    w = p.getWedge()
    r = ref.getVolume()
    a = lowpassFilter(w.apply(v, p.getRotation().invert()), freq)[0]
    b = lowpassFilter(w.apply(r, p.getRotation().invert()), freq)[0]
    s = nxcc(a, b, diff_mask.getVolume())

    return s


def paverage(particleList, norm, binning, verbose):
    from pytom_volume import read,vol
    from pytom_volume import transformSpline as transform
    from pytom.basic.structures import Particle
    from pytom.basic.normalise import mean0std1
    from pytom.tools.ProgressBar import FixedProgBar
    
    if len(particleList) == 0:
        raise RuntimeError('The particlelist provided is empty. Aborting!')
    
    if verbose:
        progressBar = FixedProgBar(0,len(particleList),'Particles averaged ')
        progressBar.update(0)
        numberAlignedParticles = 0
    
    result = None
    wedgeSum = None
    newParticle = None
    
    for particleObject in particleList:
        
        particle = read(particleObject.getFilename(), 0,0,0,0,0,0,0,0,0, binning,binning,binning)
        if norm:
            mean0std1(particle)

        wedgeInfo = particleObject.getWedge()
        
        if result is None: # initialization
            sizeX = particle.sizeX()
            sizeY = particle.sizeY()
            sizeZ = particle.sizeZ()
            
            newParticle = vol(sizeX,sizeY,sizeZ)
            
            centerX = sizeX/2 
            centerY = sizeY/2 
            centerZ = sizeZ/2 
            
            result = vol(sizeX,sizeY,sizeZ)
            result.setAll(0.0)
            
            wedgeSum = wedgeInfo.returnWedgeVolume(sizeX,sizeY,sizeZ)
            wedgeSum.setAll(0)
        

        # create spectral wedge weighting
        rotation = particleObject.getRotation()
        wedge = wedgeInfo.returnWedgeVolume(sizeX,sizeY,sizeZ,False, rotation.invert())
        
        wedgeSum += wedge
        
        # shift and rotate particle
        shiftV = particleObject.getShift()
        newParticle.setAll(0)
        transform(particle,newParticle,-rotation[1],-rotation[0],-rotation[2],
            centerX,centerY,centerZ,-shiftV[0]/binning,
        -shiftV[1]/binning,-shiftV[2]/binning,0,0,0)
        
        result += newParticle
        
        if verbose:
            numberAlignedParticles = numberAlignedParticles + 1
            progressBar.update(numberAlignedParticles)

    
    # write to the disk
    result.write('avg_'+str(mpi.rank)+'.em')
    result = Particle('avg_'+str(mpi.rank)+'.em')
    wedgeSum.write('wedge_'+str(mpi.rank)+'.em')
    wedgeSum = Particle('wedge_'+str(mpi.rank)+'.em')
    
    return (result, wedgeSum)


def calculate_averages(pl, binning, mask):
    import os
    from pytom_volume import complexDiv
    from pytom.basic.fourier import fft,ifft
    from pytom.basic.correlation import FSC, determineResolution
    from pytom_fftplan import fftShift
    from pytom_volume import reducedToFull

    pls = pl.copy().splitByClass()
    res = {}
    freqs = {}
    wedgeSum = {}
    if mask:
        from pytom_volume import read
        mask = read(mask, 0,0,0,0,0,0,0,0,0, binning,binning,binning)
    else:
        mask = None

    for pp in pls:
        # ignore the -1 class, which is used for storing the trash class
        class_label = pp[0].getClass()
        if class_label != '-1':
            assert len(pp) > 3
            if len(pp) >= 4*mpi.size:
                spp = mpi._split_seq(pp, mpi.size)
            else: # not enough particle to do averaging on one node
                spp = [None] * 2
                spp[0] = pp[:len(pp)/2]
                spp[1] = pp[len(pp)/2:]
            args = zip(spp, [True]*len(spp), [binning]*len(spp), [False]*len(spp))
            avgs = mpi.parfor(paverage, args)

            even_a, even_w, odd_a, odd_w = None, None, None, None
            even_avgs = avgs[1::2]
            odd_avgs = avgs[::2]

            for a, w in even_avgs:
                if even_a is None:
                    even_a = a.getVolume()
                    even_w = w.getVolume()
                else:
                    even_a += a.getVolume()
                    even_w += w.getVolume()
                os.remove(a.getFilename())
                os.remove(w.getFilename())

            for a, w in odd_avgs:
                if odd_a is None:
                    odd_a = a.getVolume()
                    odd_w = w.getVolume()
                else:
                    odd_a += a.getVolume()
                    odd_w += w.getVolume()
                os.remove(a.getFilename())
                os.remove(w.getFilename())

            # determine the resolution
            fsc = FSC(even_a, odd_a, even_a.sizeX()/2, mask)
            band = determineResolution(fsc, 0.5)[1]

            aa = even_a + odd_a
            ww = even_w + odd_w
            fa = fft(aa)
            r = complexDiv(fa, ww)
            rr = ifft(r)
            rr.shiftscale(0.0, 1./(rr.sizeX()*rr.sizeY()*rr.sizeZ()))
            
            res[class_label] = rr
            freqs[class_label] = band

            ww2 = reducedToFull(ww)
            fftShift(ww2, True)
            wedgeSum[class_label] = ww2

    return res, freqs, wedgeSum


def frm_proxy(p, ref, freq, offset, binning, mask):
    from pytom_volume import read
    from pytom.basic.structures import Shift, Rotation
    from sh_alignment.frm import frm_align
    v = p.getVolume(binning)
    if mask:
        mask = read(mask, 0,0,0,0,0,0,0,0,0, binning,binning,binning)
    pos, angle, score = frm_align(v, p.getWedge(), ref.getVolume(), None, [4,64], freq, offset, mask)

    return (Shift([pos[0]-v.sizeX()/2, pos[1]-v.sizeY()/2, pos[2]-v.sizeZ()/2]), Rotation(angle), score, p.getFilename())


def score_noalign_proxy(p, ref, freq, offset, binning, mask):
    from pytom.basic.structures import Shift, Rotation
    from pytom.basic.correlation import nxcc
    from pytom.basic.filter import lowpassFilter
    v = p.getTransformedVolume(binning)
    w = p.getWedge()
    r = ref.getVolume()
    a = lowpassFilter(w.apply(v, p.getRotation().invert()), freq)[0]
    b = lowpassFilter(w.apply(r, p.getRotation().invert()), freq)[0]
    score = nxcc(a, b)
    
    return (p.getShift(), p.getRotation(), score, p.getFilename())
        

def calculate_scores(pl, references, freqs, offset, binning, mask, noalign=False):
    res = {}
    for c, ref in references.iteritems():
        freq = int(freqs[c]) # get the corresponding frequency of this class
        args = zip(pl, [ref]*len(pl), [freq]*len(pl), [offset]*len(pl), [binning]*len(pl), [mask]*len(pl))
        if noalign:
            scores = mpi.parfor(score_noalign_proxy, args)
        else:
            scores = mpi.parfor(frm_proxy, args)
        
        res[c] = scores

    return res


def calculate_prob(s, scores):
    i = 0
    n = len(scores)
    for item in scores:
        sc = item[2]
        if sc >= s:
            i += 1

    return float(i)/n


def voting(p, i, scores, references, frequencies, dmaps, binning, noise):
    from collections import defaultdict

    if i in noise:
        new_label = '-1'
        return new_label

    votes = defaultdict(lambda: 0)
    class_labels = scores.keys()
    for c1, c2 in combinations(class_labels, 2):
        if dmaps.has_key((c1, c2)):
            dmap1 = dmaps[(c1, c2)][0]
            dmap2 = dmaps[(c1, c2)][1]
        elif dmaps.has_key((c2, c1)):
            dmap2 = dmaps[(c2, c1)][0]
            dmap1 = dmaps[(c2, c1)][1]
        else:
            raise Exception("No such pair in difference maps!")

        p.setShift(scores[c1][i][0])
        p.setRotation(scores[c1][i][1])
        s1 = focus_score(p, references[c1], frequencies[c1], dmap1, binning)

        p.setShift(scores[c2][i][0])
        p.setRotation(scores[c2][i][1])
        s2 = focus_score(p, references[c2], frequencies[c2], dmap2, binning)

        if s1 > s2:
            votes[c1] += 1
        else:
            votes[c2] += 1

    # count the votes and determine the class label
    peak = 0
    for c, v in votes.iteritems():
        if v > peak:
            peak = v
            new_label = c

    return new_label


def determine_class_labels(pl, references, frequencies, scores, dmaps, binning, noise_percentage=None):
    # make sure the particle list and scores have the same order
    for i, p in enumerate(pl):
        fname1 = p.getFilename()
        for label in scores.keys():
            fname2 = scores[label][i][3]
            if fname1 != fname2:
                raise Exception("Particle list and the scores do not have the same order!")

    
    from pytom.frm.FRMAlignment import FRMScore

    # track the class changes
    class_changes = {}
    class_labels = scores.keys()
    class_labels_with_noise = scores.keys()
    if not '-1' in class_labels_with_noise: # append the noise class label
        class_labels_with_noise.append('-1')
    for c1 in class_labels_with_noise:
        for c2 in class_labels_with_noise:
            class_changes[(c1, c2)] = 0

    # calculate the probabilities of being noise class
    if noise_percentage:
        noise_prob_distribution = []
        for i in xrange(len(pl)):
            prob = 1.
            for label in scores.keys():
                prob *= calculate_prob(scores[label][i][2], scores[label])
            noise_prob_distribution.append(prob)
        prob_order = np.argsort(noise_prob_distribution)
        noise = prob_order[-int(noise_percentage*len(pl)):]
    else:
        noise = []

    # determine the class labels by voting
    args = zip(pl, range(len(pl)), [scores]*len(pl), [references]*len(pl), [frequencies]*len(pl), [dmaps]*len(pl), [binning]*len(pl), [noise]*len(pl))
    new_labels = mpi.parfor(voting, args)
    for i, p in enumerate(pl):
        old_label = p.getClass()
        new_label = new_labels[i]

        # set the corresponding info
        p.setClass(new_label)
        if new_label == '-1':
            pass
        else:
            p.setShift(scores[new_label][i][0])
            p.setRotation(scores[new_label][i][1])
            p.setScore(FRMScore(scores[new_label][i][2]))

        # track the changes
        if class_changes.has_key((old_label, new_label)):
            class_changes[(old_label, new_label)] += 1


    # print the changes
    print "Class changes:"
    print "   ",
    for c in class_labels_with_noise:
        print "%3s" % str(c),
    print
    for c1 in class_labels_with_noise:
        print "%3s" % str(c1),
        for c2 in class_labels_with_noise:
            print "%3d" % class_changes[(c1, c2)],
        print

    return pl


def split_topn_classes(pls, n):
    # sort the particle list by the length
    assert len(pls) >= n
    pls.sort(lambda x,y: -cmp(len(x), len(y)))

    # find the maximal running label
    max_label = -1
    for pp in pls:
        cl = int(pp[0].getClass())
        if cl > max_label:
            max_label = cl

    # split according to the score
    new_pls = []
    i = 0
    for pp in pls:
        class_label = pp[0].getClass()
        if class_label == '-1': # we do nothing with the trash class
            new_pls.append(pp)
            continue

        if i<n:
            pp.sortByScore()
            l = len(pp)
            p1 = pp[:l/2]
            p2 = pp[l/2:]

            p1.setClassAllParticles(str(max_label+1))
            p2.setClassAllParticles(str(max_label+2))
            new_pls.append(p1)
            new_pls.append(p2)
            print "Split class %s to %s and %s" % (class_label, str(max_label+1), str(max_label+2))
            max_label += 2

            i += 1
        else:
            new_pls.append(pp)

    # return the result
    new_pl = ParticleList()
    for pp in new_pls:
        new_pl += pp

    return new_pl


def compare_pl(old_pl, new_pl):
    n = len(old_pl)
    assert n == len(new_pl)

    ndiff = 0
    for p in old_pl:
        pp = new_pl.getParticleByFilename(p.getFilename())
        if p.getClassName() != pp.getClassName():
            ndiff += 1

    if float(ndiff)/n < 0.005: # stopping criterion
        return True
    else:
        return False


def distance(p, ref, freq, mask, binning):
    from pytom.basic.correlation import nxcc
    from pytom_volume import vol, initSphere, read
    from pytom.basic.filter import lowpassFilter

    v = p.getTransformedVolume(binning)
    w = p.getWedge()
    r = ref.getVolume()
    a = lowpassFilter(w.apply(v, p.getRotation().invert()), freq)[0]
    b = lowpassFilter(w.apply(r, p.getRotation().invert()), freq)[0]

    if not mask:
        mask = vol(r)
        initSphere(mask, r.sizeX()/2-3, 3, 0, r.sizeX()/2, r.sizeY()/2, r.sizeZ()/2)
    else:
        mask = read(mask, 0,0,0,0,0,0,0,0,0, binning,binning,binning)

    s = nxcc(a, b, mask)
    d2 = 2*(1-s)

    return d2


def initialize(pl, settings):
    from pytom.basic.structures import Particle
    # from pytom.alignment.alignmentFunctions import average2
    from pytom.basic.filter import lowpassFilter

    print "Initializing the class centroids ..."
    pl = pl.copy()
    pl.sortByScore()
    if settings["noise"]:
        pl = pl[:int((1-settings["noise"])*len(pl))]

    K = settings["ncluster"]
    freq = settings["frequency"]
    kn = len(pl)/K
    references = {}
    frequencies = {}

    # get the first class centroid
    pp = pl[:kn]
    # avg, fsc = average2(pp, norm=True, verbose=False)
    pp.setClassAllParticles('0')
    res, tmp, tmp2 = calculate_averages(pp, settings["binning"], None)
    avg = res['0']
    avg = lowpassFilter(avg, freq, freq/10.)[0]
    avg.write('initial_0.em')
    p = Particle('initial_0.em')
    p.setClass('0')
    references['0'] = p
    frequencies['0'] = freq

    for k in xrange(1, K):
        distances = [4]*len(pl)
        for c, ref in references.iteritems():
            args = zip(pl, [ref]*len(pl), [freq]*len(pl), [settings["fmask"]]*len(pl), [settings["binning"]]*len(pl))
            dist = mpi.parfor(distance, args)
            for i in xrange(len(pl)):
                if distances[i] > dist[i]:
                    distances[i] = dist[i]

        distances = np.asarray(distances)
        distances = distances/np.sum(distances)
        idx = np.random.choice(len(pl), kn, replace=False, p=distances)
        pp = ParticleList()
        for i in idx:
            pp.append(pl[int(i)])
        # avg, fsc = average2(pp, norm=True, verbose=False)
        pp.setClassAllParticles('0')
        res, tmp, tmp2 = calculate_averages(pp, settings["binning"], None)
        avg = res['0']
        avg = lowpassFilter(avg, freq, freq/10.)[0]
        avg.write('initial_'+str(k)+'.em')
        p = Particle('initial_'+str(k)+'.em')
        p.setClass(str(k))
        references[str(k)] = p
        frequencies[str(k)] = freq
    
    return references, frequencies


def classify(pl, settings):
    from pytom.basic.structures import Particle, Shift, Rotation
    from pytom.basic.filter import lowpassFilter
    
    # make the particle list picklable
    pl.pickle()

    # define the starting status
    offset = settings["offset"]
    binning = settings["binning"]
    mask = settings["mask"]
    sfrequency = settings["frequency"] # starting frequency

    references = {}
    frequencies = {}
    ncluster = 0
    if settings["external"]: # use external references
        for class_label, fname in enumerate(settings["external"]):
            p = Particle(fname)
            p.setClass(str(class_label))
            references[str(class_label)] = p
            frequencies[str(class_label)] = sfrequency
            ncluster += 1
    else:
        if not settings["resume"]:
            if not settings["ncluster"]:
                print "Must specify the number of clusters!"
                return

            # k-means++ way to initialize
            ncluster = settings["ncluster"]
            references, frequencies = initialize(pl, settings)
        else:
            avgs, tmp, tmp2 = calculate_averages(pl, binning, mask)

            for class_label, r in avgs.iteritems():
                fname = 'initial_class'+str(class_label)+'.em'
                rr = lowpassFilter(r, sfrequency, sfrequency/10.)[0]
                rr.write(fname)
                p = Particle(fname)
                p.setClass(str(class_label))
                references[str(class_label)] = p
                frequencies[str(class_label)] = sfrequency
                ncluster += 1

    # start the classification
    for i in xrange(settings["niteration"]):
        if ncluster < 2:
            print 'Not enough number of clusters. Exit!'
            break

        print "Starting iteration %d ..." % i
        old_pl = pl.copy()

        # compute the difference maps
        print "Calculate difference maps ..."
        args = []
        for pair in combinations(references.keys(), 2):
            args.append((references[pair[0]], frequencies[pair[0]], references[pair[1]], frequencies[pair[1]], mask, settings["fmask"], binning, i, settings["sigma"], settings["threshold"]))

        dmaps = {}
        res = mpi.parfor(calculate_difference_map_proxy, args)
        for r in res:
            dmaps[(r[0].getClass(), r[1].getClass())] = r


        # start the alignments
        print "Start alignments ..."
        scores = calculate_scores(pl, references, frequencies, offset, binning, mask, settings["noalign"])

        # determine the class labels & track the class changes
        pl = determine_class_labels(pl, references, frequencies, scores, dmaps, binning, settings["noise"])

        # kick out the small classes
        pls = pl.copy().splitByClass()
        nlabels = {}
        for pp in pls:
            nlabels[pp[0].getClass()] = len(pp)
            print "Number of class " + str(pp[0].getClass()) + ": " + str(len(pp))

        max_labels = np.max(nlabels.values())
        to_delete = []
        if settings["dispersion"]:
            min_labels = float(max_labels)/settings["dispersion"]
            for key, value in nlabels.iteritems():
                if value <= min_labels:
                    to_delete.append(key)

        for pp in pls:
            if pp[0].getClass() in to_delete:
                pp.setClassAllParticles('-1')
                print "Set class " + str(pp[0].getClass()) + " to noise"

        # split the top n classes
        pl = split_topn_classes(pls, len(to_delete))
        
        # update the references
        print "Calculate averages ..."
        avgs, freqs, wedgeSum = calculate_averages(pl, binning, mask)
        ncluster = 0
        references = {}
        for class_label, r in avgs.iteritems():
            if not settings["fixed_frequency"]:
                freq = freqs[str(class_label)]
            else:
                freq = sfrequency
            frequencies[str(class_label)] = int(freq)
            print 'Resolution of class %s: %d' % (str(class_label), freq)

            fname = 'iter'+str(i)+'_class'+str(class_label)+'.em'
            rr = lowpassFilter(r, freq, freq/10.)[0]
            rr.write(fname)
            p = Particle(fname)
            p.setClass(str(class_label))
            references[str(class_label)] = p
            ncluster += 1

            w = wedgeSum[str(class_label)]
            fname = 'iter'+str(i)+'_class'+str(class_label)+'_wedge.em'
            w.write(fname)

        # write the result to the disk
        pl.toXMLFile('classified_pl_iter'+str(i)+'.xml')
        
        # check the stopping criterion
        if compare_pl(old_pl, pl):
            break



if __name__ == '__main__':
    # parse args
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option("-p", dest="filename",
                      help="Filename of ParticleList", metavar="FILE")
    parser.add_option("-k", dest="k",
                      help="Number of classes")
    parser.add_option("-f", dest="frequency",
                      help="Maximal frequency (in pixel) involved in score calculation")
    parser.add_option("-s", dest="offset",
                      help="Potential offset of the particle (optional)")
    parser.add_option("-b", dest="binning",
                      help="Binning factor (optional)")
    parser.add_option("-m", dest="mask",
                      help="Alignment mask (optional)")
    parser.add_option("-c", dest="fmask",
                      help="Focused classification mask (optional)")
    parser.add_option("-i", dest="niteration",
                      help="Number of iterations to run (optional)")
    parser.add_option("-d", dest="dispersion",
                      help="Maximal allowed cluster dispersion (optional)")
    parser.add_option("-r", dest="external",
                      help="Use external references (optional)")
    parser.add_option("-l", dest="resume", action="store_true",
                      help="Start with the class assignment stored in particle list (optional)")
    parser.add_option("-n", dest="noise",
                      help="Noise percentage (optional)")
    parser.add_option("-g", dest="sigma",
                      help="Particle density threshold for difference map (optional)")
    parser.add_option("-t", dest="threshold",
                      help="STD threshold for difference map, by default 0.4 (optional)")
    parser.add_option("-a", dest="noalign", action="store_true",
                      help="Run without alignment (optional)")


    (options, args) = parser.parse_args()

    assert options.filename
    assert options.frequency

    settings = {}
    settings["ncluster"] = int(options.k) if options.k else None
    settings["frequency"] = int(options.frequency)
    settings["fixed_frequency"] = True
    settings["offset"] = int(options.offset) if options.offset else None
    settings["binning"] = int(options.binning) if options.binning else 1
    settings["mask"] = options.mask
    settings["fmask"] = options.fmask
    settings["niteration"] = int(options.niteration) if options.niteration else 10
    settings["dispersion"] = int(options.dispersion) if options.dispersion else None
    settings["external"] = options.external.split(',') if options.external else None
    settings["resume"] = options.resume
    settings["sigma"] = float(options.sigma) if options.sigma else None
    settings["threshold"] = float(options.threshold) if options.threshold else 0.4
    settings["noise"] = float(options.noise) if options.noise else None
    settings["noalign"] = options.noalign
    if settings["noise"]:
        assert settings["noise"] > 0 and settings["noise"] < 1


    # start the clustering
    mpi.begin()

    try:
        pl = ParticleList()
        pl.fromXMLFile(options.filename)

        classify(pl, settings)
    except Exception, e:
        print e
    finally:
        mpi.end()

